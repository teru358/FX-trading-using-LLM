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
