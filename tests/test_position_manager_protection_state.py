"""Order protection state fields and compatibility."""
from __future__ import annotations

from datetime import datetime

from src.trading.position_manager import Order, PositionManager


def test_order_new_initializes_protection_state_fields() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0)

    assert order.max_favorable_price is None
    assert order.max_favorable_r == 0.0
    assert order.initial_risk_price_distance == 0.5
    assert order.last_protection_stage == "none"
    assert order.pending_protection_sl is None
    assert order.pending_protection_reason == ""
    assert order.pending_protection_updated_at is None
    assert order.last_reversal_guard_at is None
    assert order.reversal_guard_count == 0


def test_order_from_dict_backfills_protection_state_fields() -> None:
    legacy = {
        "order_id": "legacy-1",
        "pair": "USDJPY=X",
        "direction": "buy",
        "entry_price": 160.0,
        "stop_loss": 159.5,
        "take_profit": 161.0,
        "position_size": 1000.0,
        "initial_stop_loss": 159.0,
        "status": "open",
        "opened_at": "2026-06-13T10:00:00",
        "closed_at": None,
        "close_price": None,
        "close_reason": None,
        "realized_pnl": None,
        "signal_reason": "",
    }

    order = Order.from_dict(legacy)

    assert order.initial_risk_price_distance == 1.0
    assert order.max_favorable_price is None
    assert order.max_favorable_r == 0.0
    assert order.last_protection_stage == "none"
    assert order.pending_protection_sl is None
    assert order.pending_protection_reason == ""
    assert order.pending_protection_updated_at is None
    assert order.reversal_guard_count == 0


def test_order_protection_datetimes_roundtrip() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0)
    order.pending_protection_sl = 160.0
    order.pending_protection_reason = "reversal_guard"
    order.pending_protection_updated_at = datetime(2026, 6, 13, 10, 5)
    order.last_reversal_guard_at = datetime(2026, 6, 13, 10, 0)
    order.reversal_guard_count = 2

    restored = Order.from_dict(order.to_dict())

    assert restored.pending_protection_updated_at == datetime(2026, 6, 13, 10, 5)
    assert restored.last_reversal_guard_at == datetime(2026, 6, 13, 10, 0)
    assert restored.pending_protection_sl == 160.0
    assert restored.reversal_guard_count == 2


def test_position_manager_updates_protection_state(tmp_state_store) -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0)
    pm = PositionManager(tmp_state_store)
    pm.open_position(order)

    ok = pm.update_protection_state(
        order.order_id,
        max_favorable_price=160.4,
        max_favorable_r=0.8,
        last_protection_stage="breakeven",
    )

    assert ok is True
    pos = pm.get_open_position("USDJPY=X")
    assert pos is not None
    assert pos.max_favorable_price == 160.4
    assert pos.max_favorable_r == 0.8
    assert pos.last_protection_stage == "breakeven"


def test_position_manager_sets_and_clears_pending_protection_target(tmp_state_store) -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.5, 161.0, 1000.0)
    pm = PositionManager(tmp_state_store)
    pm.open_position(order)

    assert pm.set_pending_protection_target(order.order_id, 160.0, "reversal_guard") is True
    pos = pm.get_open_position("USDJPY=X")
    assert pos is not None
    assert pos.pending_protection_sl == 160.0
    assert pos.pending_protection_reason == "reversal_guard"
    assert pos.pending_protection_updated_at is not None

    assert pm.clear_pending_protection_target(order.order_id) is True
    pos = pm.get_open_position("USDJPY=X")
    assert pos is not None
    assert pos.pending_protection_sl is None
    assert pos.pending_protection_reason == ""
    assert pos.pending_protection_updated_at is None
