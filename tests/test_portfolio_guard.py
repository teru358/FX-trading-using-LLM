"""portfolio_guard.check_portfolio_limits() のテスト。"""
from __future__ import annotations

from src.trading.portfolio_guard import check_portfolio_limits
from src.trading.position_manager import Order


def _pos(pair: str, direction: str = "buy") -> Order:
    return Order.new(
        pair=pair,
        direction=direction,
        entry_price=150.0,
        stop_loss=149.0,
        take_profit=152.0,
        position_size=10000.0,
    )


def test_allows_when_no_positions():
    result = check_portfolio_limits(
        pair="USDJPY=X", direction="buy", position_size=10000,
        open_positions=[],
    )
    assert result is None


def test_blocks_when_max_total_reached():
    positions = [_pos("USDJPY=X"), _pos("EURUSD=X"), _pos("GBPUSD=X"), _pos("AUDUSD=X")]
    result = check_portfolio_limits(
        pair="USDCHF=X", direction="buy", position_size=10000,
        open_positions=positions, max_total_positions=4,
    )
    assert result is not None
    assert "Max total positions" in result


def test_blocks_when_currency_group_full():
    positions = [_pos("USDJPY=X"), _pos("EURJPY=X")]
    result = check_portfolio_limits(
        pair="GBPJPY=X", direction="buy", position_size=10000,
        open_positions=positions, max_positions_per_group=2,
    )
    assert result is not None
    assert "JPY group" in result


def test_allows_different_group():
    """JPYグループが埋まっていても、別グループのペアは開ける。"""
    positions = [_pos("USDJPY=X"), _pos("EURJPY=X")]
    result = check_portfolio_limits(
        pair="AUDUSD=X", direction="buy", position_size=10000,
        open_positions=positions, max_positions_per_group=2,
    )
    # AUDUSD is in USD group, not JPY-only
    # USD group has USDJPY (1 position) → still under limit
    assert result is None


def test_blocks_same_direction_in_group():
    positions = [_pos("USDJPY=X", "buy"), _pos("EURJPY=X", "buy")]
    result = check_portfolio_limits(
        pair="GBPJPY=X", direction="buy", position_size=10000,
        open_positions=positions,
        max_positions_per_group=3,  # enough room by count
        max_same_direction_per_group=2,
    )
    assert result is not None
    assert "buy positions" in result


def test_allows_opposite_direction_in_group():
    """グループ内に2つbuyがあっても、sellなら同方向制限に引っかからない。"""
    positions = [_pos("USDJPY=X", "buy"), _pos("EURJPY=X", "buy")]
    result = check_portfolio_limits(
        pair="GBPJPY=X", direction="sell", position_size=10000,
        open_positions=positions,
        max_positions_per_group=3,
        max_same_direction_per_group=2,
    )
    assert result is None
