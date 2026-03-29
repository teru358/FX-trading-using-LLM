"""position_manager.PositionManager のテスト。"""
from __future__ import annotations

import pytest

from src.trading.position_manager import Order, PositionManager


def test_open_position(tmp_state_store, buy_order):
    """open_position 後にポジションが open_positions に追加される。"""
    pm = PositionManager(tmp_state_store, initial_balance=100_000.0, context="Test")
    pm.open_position(buy_order)
    account = pm.get_account_state()
    assert len(account.open_positions) == 1
    assert account.open_positions[0].pair == "USDJPY=X"
    assert account.open_positions[0].direction == "buy"


def test_close_position_profit(tmp_state_store, buy_order):
    """利益決済: close_price > entry_price → realized_pnl > 0、balance が増加する。"""
    pm = PositionManager(tmp_state_store, initial_balance=100_000.0, context="Test")
    pm.open_position(buy_order)

    close_price = 151.0  # entry=150.0 → +1.0 × 10000 = +10000
    closed = pm.close_position(buy_order.order_id, close_price, "take_profit")

    assert closed is not None
    assert closed.realized_pnl == pytest.approx(10_000.0)
    account = pm.get_account_state()
    assert account.balance == pytest.approx(110_000.0)
    assert len(account.open_positions) == 0
    assert len(account.closed_trades) == 1


def test_close_position_loss(tmp_state_store, buy_order):
    """損失決済: close_price < entry_price → realized_pnl < 0、balance が減少する。"""
    pm = PositionManager(tmp_state_store, initial_balance=100_000.0, context="Test")
    pm.open_position(buy_order)

    close_price = 149.0  # entry=150.0 → -1.0 × 10000 = -10000
    closed = pm.close_position(buy_order.order_id, close_price, "stop_loss")

    assert closed is not None
    assert closed.realized_pnl == pytest.approx(-10_000.0)
    account = pm.get_account_state()
    assert account.balance == pytest.approx(90_000.0)


def test_close_nonexistent_position(tmp_state_store):
    """存在しない order_id を close しようとすると None が返る。"""
    pm = PositionManager(tmp_state_store, initial_balance=100_000.0, context="Test")
    result = pm.close_position("nonexistent-id", 150.0, "manual")
    assert result is None


def test_get_account_state(tmp_state_store, buy_order, sell_order):
    """複数ポジションを追加後、get_account_state が正しい値を返す。"""
    pm = PositionManager(tmp_state_store, initial_balance=100_000.0, context="Test")
    pm.open_position(buy_order)
    pm.open_position(sell_order)

    account = pm.get_account_state()
    assert account.balance == pytest.approx(100_000.0)
    assert account.initial_balance == pytest.approx(100_000.0)
    assert len(account.open_positions) == 2
    assert account.total_trades == 0
