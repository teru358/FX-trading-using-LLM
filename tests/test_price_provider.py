"""price_provider のユニットテスト。"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.data.price_fetcher import CurrentPrice, PriceData
from src.data.price_provider import PriceProvider


def _make_config(provider: str = "yfinance"):
    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.paper_provider = provider
    cfg.live_broker = None
    if provider == "twelvedata":
        cfg.providers.twelvedata = MagicMock(
            daily_limit=800,
            per_minute_limit=8,
            use_for_monitor=True,
            indices=[],
        )
    else:
        cfg.providers.twelvedata = None
    cfg.providers.mt5 = None
    cfg.trading.lookback_days = 90
    cfg.trading.ohlcv_interval = "1h"
    cfg.tradeable_instruments = [
        MagicMock(symbol="USDJPY=X", asset_type="fx"),
    ]
    cfg.price_monitor.interval_minutes = 5
    cfg.schedule.run_times = ["09:30", "15:00", "21:30"]
    return cfg


class TestEstimateDaily:

    def test_estimate_yfinance(self):
        cfg = _make_config("yfinance")
        provider = PriceProvider(cfg)
        assert provider.estimate_daily_requests() == 0

    def test_estimate_twelvedata(self):
        cfg = _make_config("twelvedata")
        with patch.dict("os.environ", {"TWELVEDATA_API_KEY": "fake"}):
            provider = PriceProvider(cfg)
        est = provider.estimate_daily_requests()
        assert est > 0


class TestFallback:

    def test_is_trade_pair(self):
        cfg = _make_config("twelvedata")
        with patch.dict("os.environ", {"TWELVEDATA_API_KEY": "fake"}):
            provider = PriceProvider(cfg)
        assert provider._is_trade_pair("USDJPY=X") is True
        assert provider._is_trade_pair("^N225") is False


def _make_mt5_config():
    """live_broker=mt5 の最小スタブ。"""
    cfg = MagicMock()
    cfg.mode = "live"
    cfg.paper_provider = "yfinance"
    cfg.live_broker = "mt5"
    cfg.providers.twelvedata = None
    mt5 = MagicMock()
    mt5.bridge_url = "http://x:8812"
    mt5.request_timeout_seconds = 5.0
    mt5.api_key = ""
    cfg.providers.mt5 = mt5
    cfg.trading.lookback_days = 90
    cfg.trading.ohlcv_interval = "1h"
    cfg.tradeable_instruments = [MagicMock(symbol="USDJPY=X", asset_type="fx")]
    cfg.price_monitor.interval_minutes = 5
    cfg.schedule.run_times = ["09:30"]
    return cfg


def test_get_current_price_uses_mt5_for_trade_pairs():
    """live_broker=mt5 + trade pair → MT5 fetcher が呼ばれる (TD/yfinance より優先)。"""
    cfg = _make_mt5_config()
    provider = PriceProvider(cfg)
    assert provider._mt5_enabled is True

    expected = CurrentPrice(price=153.42, timestamp=datetime.now())
    provider._mt5_fetcher = MagicMock()
    provider._mt5_fetcher.fetch_current_price.return_value = expected

    cp = provider.get_current_price("USDJPY=X", is_monitor=True)

    assert cp is expected
    provider._mt5_fetcher.fetch_current_price.assert_called_once_with("USDJPY=X")


def test_get_current_price_falls_back_when_mt5_unreachable():
    """MT5 fetcher 失敗時は Twelve Data → yfinance にフォールバック (halt 関与なし)。"""
    from src.data.mt5_ohlcv_fetcher import Mt5UnreachableError
    from unittest.mock import patch

    cfg = _make_mt5_config()
    provider = PriceProvider(cfg)

    provider._mt5_fetcher = MagicMock()
    provider._mt5_fetcher.fetch_current_price.side_effect = Mt5UnreachableError("down")

    fallback_price = CurrentPrice(price=152.99, timestamp=datetime.now())
    with patch(
        "src.data.price_provider.fetch_current_price", return_value=fallback_price,
    ):
        cp = provider.get_current_price("USDJPY=X", is_monitor=True)

    assert cp is fallback_price
    provider._mt5_fetcher.fetch_current_price.assert_called_once()


def test_get_current_price_skips_mt5_for_watch_only_symbols():
    """trade ペアでないシンボルは MT5 を経由しない (TD/yfinance のみ)。"""
    cfg = _make_mt5_config()
    provider = PriceProvider(cfg)

    provider._mt5_fetcher = MagicMock()  # 呼ばれてはいけない

    fallback_price = CurrentPrice(price=200.0, timestamp=datetime.now())
    with patch(
        "src.data.price_provider.fetch_current_price", return_value=fallback_price,
    ):
        cp = provider.get_current_price("SPY", is_monitor=True)  # watch only

    assert cp is fallback_price
    provider._mt5_fetcher.fetch_current_price.assert_not_called()
