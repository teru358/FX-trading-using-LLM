"""price_fetcher の CurrentPrice テスト。"""
from datetime import datetime

from src.data.price_fetcher import CurrentPrice


def test_current_price_basic_fields():
    cp = CurrentPrice(price=150.0, timestamp=datetime(2026, 3, 31, 12, 0))
    assert cp.price == 150.0
    assert cp.timestamp == datetime(2026, 3, 31, 12, 0)
    assert cp.percent_change is None
    assert cp.rolling_1d_change is None
    assert cp.fifty_two_week_high is None
    assert cp.is_market_open is None


def test_current_price_with_extras():
    cp = CurrentPrice(
        price=150.0,
        timestamp=datetime(2026, 3, 31, 12, 0),
        percent_change=-0.5,
        rolling_1d_change=-0.3,
        rolling_7d_change=1.2,
        fifty_two_week_high=160.0,
        fifty_two_week_low=130.0,
        is_market_open=True,
    )
    assert cp.percent_change == -0.5
    assert cp.fifty_two_week_high == 160.0
    assert cp.is_market_open is True


def test_current_price_float_backward_compat():
    """float() で従来と同じように price 値を取得できること。"""
    cp = CurrentPrice(price=149.85, timestamp=datetime.now())
    assert float(cp) == 149.85


import pandas as pd
from zoneinfo import ZoneInfo
import src.utils.clock as clock
from src.data.price_fetcher import _normalize_index

_JST = ZoneInfo("Asia/Tokyo")


def test_normalize_index_aware_utc_to_local(monkeypatch):
    """tz-aware UTC index は naive ローカルに変換される (naive UTC のままにしない)。"""
    monkeypatch.setattr(clock, "_resolve_local_tz", lambda tz=None: _JST)
    idx = pd.DatetimeIndex(["2026-06-29 10:00:00"], tz="UTC")
    df = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0],
                       "Close": [1.0], "Volume": [1]}, index=idx)
    out = _normalize_index(df)
    assert out.index.tz is None
    assert out.index[-1] == pd.Timestamp("2026-06-29 19:00:00")  # JST
