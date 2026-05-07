"""PriceProvider の MT5 → TD → yfinance chain + degraded/recovery 通知テスト。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd

from src.data.mt5_ohlcv_fetcher import Mt5UnreachableError
from src.data.price_fetcher import PriceData
from src.data.price_provider import PriceProvider


def _make_config(mt5_url: str = "http://x:8812"):
    cfg = MagicMock()
    if mt5_url:
        cfg.mode = "live_test"
        cfg.live_broker = "mt5"
        cfg.providers.mt5 = MagicMock(
            bridge_url=mt5_url,
            request_timeout_seconds=5.0,
        )
    else:
        cfg.mode = "paper"
        cfg.live_broker = None
        cfg.providers.mt5 = None
    cfg.paper_provider = "yfinance"  # TD 無効化で簡素化
    cfg.providers.twelvedata = None
    cfg.tradeable_instruments = [MagicMock(symbol="USDJPY=X")]
    return cfg


def _fake_price_data() -> PriceData:
    from datetime import datetime
    df = pd.DataFrame({
        "Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0],
    }, index=pd.to_datetime(["2026-04-29"]))
    return PriceData(symbol="USDJPY=X", df=df, current_price=1.0, fetched_at=datetime.now())


def test_mt5_success_falls_back_to_yfinance_when_mt5_unreachable(monkeypatch):
    """MT5 unreachable → yfinance まで降格して PriceData を返す。"""
    cfg = _make_config()
    pp = PriceProvider(cfg)
    pp._mt5_fetcher = MagicMock()
    pp._mt5_fetcher.fetch = MagicMock(side_effect=Mt5UnreachableError("down"))

    monkeypatch.setattr(
        "src.data.price_provider.fetch_ohlcv",
        lambda *a, **kw: _fake_price_data(),
    )

    data = pp.get_ohlcv("USDJPY=X")
    assert data is not None
    assert pp._active_provider["USDJPY=X"] == "yfinance"


def test_no_mt5_fetcher_when_no_bridge_url():
    """bridge_url 未設定なら _mt5_fetcher は None で、MT5 を試行しない。"""
    cfg = _make_config(mt5_url="")
    pp = PriceProvider(cfg)
    assert pp._mt5_fetcher is None
