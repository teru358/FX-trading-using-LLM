"""price_provider のユニットテスト。"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.data.price_fetcher import CurrentPrice, PriceData
from src.data.price_provider import PriceProvider


def _make_config(provider: str = "yfinance"):
    cfg = MagicMock()
    cfg.price_provider.realtime_provider = provider
    cfg.price_provider.twelvedata.daily_limit = 800
    cfg.price_provider.twelvedata.per_minute_limit = 8
    cfg.price_provider.twelvedata.watch_symbols = []
    cfg.price_provider.twelvedata.use_for_monitor = True
    cfg.trading.lookback_days = 90
    cfg.trading.ohlcv_interval = "1h"
    cfg.tradeable_instruments = [
        MagicMock(symbol="USDJPY=X", asset_type="fx"),
    ]
    cfg.price_monitor.interval_minutes = 5
    cfg.schedule.run_times = ["09:30", "15:00", "21:30"]
    # Phase 3b: MT5 chain config (default: bridge 無効)
    cfg.mt5_bridge.bridge_url = ""
    cfg.mt5_bridge.request_timeout_seconds = 5.0
    cfg.mt5_bridge.fallback.failure_window_sec = 300
    cfg.mt5_bridge.fallback.failure_threshold = 2
    cfg.mt5_bridge.fallback.heartbeat_interval_degraded_min = 15
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
