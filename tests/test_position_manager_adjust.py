"""PositionManager.adjust_position_size() のユニットテスト。"""
from __future__ import annotations

import pytest

from src.persistence.state_store import StateStore
from src.trading.position_manager import Order, PositionManager


def _setup(tmp_path) -> tuple[PositionManager, Order]:
    """PositionManager を作って 1000 unit の position を open し (manager, order) を返す。"""
    store = StateStore(tmp_path)
    pm = PositionManager(store, 100_000.0, context="test")
    order = Order.new(
        pair="USDJPY=X", direction="buy",
        entry_price=159.0, stop_loss=158.0, take_profit=160.0,
        position_size=1000.0,
    )
    pm.open_position(order)
    return pm, order


def test_adjust_reduces_size(tmp_path):
    pm, order = _setup(tmp_path)
    result = pm.adjust_position_size(order.order_id, 700.0)
    assert result is not None
    assert result.position_size == 700.0
    # 価格レベルは不変
    assert result.entry_price == 159.0
    assert result.stop_loss == 158.0
    assert result.take_profit == 160.0


def test_adjust_to_zero_raises(tmp_path):
    pm, order = _setup(tmp_path)
    with pytest.raises(ValueError, match="<= 0"):
        pm.adjust_position_size(order.order_id, 0)


def test_adjust_negative_raises(tmp_path):
    pm, order = _setup(tmp_path)
    with pytest.raises(ValueError, match="<= 0"):
        pm.adjust_position_size(order.order_id, -1)


def test_adjust_larger_raises(tmp_path):
    pm, order = _setup(tmp_path)
    with pytest.raises(ValueError, match="larger"):
        pm.adjust_position_size(order.order_id, 1500)


def test_adjust_unknown_returns_none(tmp_path):
    pm, _order = _setup(tmp_path)
    result = pm.adjust_position_size("mt5:nonexistent", 500)
    assert result is None


def test_adjust_persists_to_state_store(tmp_path):
    pm, order = _setup(tmp_path)
    pm.adjust_position_size(order.order_id, 700.0)
    # 新しい PositionManager で読み直して 700.0 になっていることを確認
    store = StateStore(tmp_path)
    pm2 = PositionManager(store, 100_000.0, context="test2")
    pos = pm2.get_open_position("USDJPY=X")
    assert pos is not None
    assert pos.position_size == 700.0


def test_balance_unchanged_after_adjust(tmp_path):
    pm, order = _setup(tmp_path)
    initial_balance = pm.get_account_state().balance
    pm.adjust_position_size(order.order_id, 700.0)
    assert pm.get_account_state().balance == initial_balance
