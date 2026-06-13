"""Pure MFE/R and profit protection calculations."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.trading.position_manager import Order
from src.trading.position_protection import (
    compute_mfe_update,
    compute_profit_protection_action,
    current_r,
    more_protective_sl,
    risk_distance,
)


def _cfg(**overrides):
    values = {
        "protect_half_r": 0.3,
        "protect_breakeven_r": 0.5,
        "protect_lock_r": 1.0,
        "giveback_close_r": 0.4,
        "giveback_close_min_mfe_r": 0.8,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_current_r_buy_and_sell() -> None:
    buy = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    sell = Order.new("USDJPY=X", "sell", 160.0, 161.0, 158.0, 1000.0)

    assert risk_distance(buy) == pytest.approx(1.0)
    assert current_r(buy, 160.5) == pytest.approx(0.5)
    assert current_r(sell, 159.5) == pytest.approx(0.5)
    assert current_r(sell, 160.5) == pytest.approx(-0.5)


def test_mfe_update_only_moves_favorably_for_buy() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    order.max_favorable_price = 160.6
    order.max_favorable_r = 0.6

    update = compute_mfe_update(order, 160.4)

    assert update.max_favorable_price == 160.6
    assert update.max_favorable_r == pytest.approx(0.6)
    assert update.current_r == pytest.approx(0.4)
    assert update.giveback_r == pytest.approx(0.2)


def test_mfe_update_moves_favorably_for_sell() -> None:
    order = Order.new("USDJPY=X", "sell", 160.0, 161.0, 158.0, 1000.0)

    update = compute_mfe_update(order, 159.4)

    assert update.max_favorable_price == 159.4
    assert update.max_favorable_r == pytest.approx(0.6)
    assert update.current_r == pytest.approx(0.6)
    assert update.giveback_r == pytest.approx(0.0)


def test_profit_protection_half_r_target() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)

    action = compute_profit_protection_action(order, 160.3, _cfg())

    assert action.action == "raise_sl"
    assert action.stage == "half"
    assert action.target_sl == pytest.approx(159.5)


def test_profit_protection_breakeven_target() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)

    action = compute_profit_protection_action(order, 160.5, _cfg())

    assert action.action == "raise_sl"
    assert action.stage == "breakeven"
    assert action.target_sl == pytest.approx(160.0)


def test_profit_protection_lock_target() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)

    action = compute_profit_protection_action(order, 161.0, _cfg())

    assert action.action == "raise_sl"
    assert action.stage == "lock"
    assert action.target_sl == pytest.approx(160.3)


def test_profit_protection_giveback_close_requires_min_mfe() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    order.max_favorable_price = 161.0
    order.max_favorable_r = 1.0

    action = compute_profit_protection_action(order, 160.55, _cfg())

    assert action.action == "close"
    assert action.stage == "giveback"
    assert "giveback" in action.reason


def test_profit_protection_does_not_worsen_current_sl() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    order.stop_loss = 160.2

    action = compute_profit_protection_action(order, 160.5, _cfg())

    assert action.action == "none"
    assert action.target_sl is None


def test_more_protective_sl_buy_and_sell() -> None:
    buy = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    sell = Order.new("USDJPY=X", "sell", 160.0, 161.0, 158.0, 1000.0)

    assert more_protective_sl(buy, 160.1, 160.3) == pytest.approx(160.3)
    assert more_protective_sl(sell, 159.9, 159.7) == pytest.approx(159.7)
    assert more_protective_sl(buy, None, 160.3) == pytest.approx(160.3)


def test_zero_initial_risk_returns_no_action() -> None:
    order = Order.new("USDJPY=X", "buy", 160.0, 160.0, 162.0, 1000.0)

    assert risk_distance(order) == 0.0
    assert current_r(order, 160.5) == 0.0
    assert compute_profit_protection_action(order, 160.5, _cfg()).action == "none"
