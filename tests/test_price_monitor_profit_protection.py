"""price_monitor profit protection and pending protection target behavior."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.trading.position_manager import Order, PositionManager
from src.utils.clock import db_now


def _make_config(mode="live"):
    cfg = MagicMock()
    cfg.mode = mode
    cfg.price_monitor.enabled = True
    cfg.price_monitor.trailing_stop_enabled = False
    cfg.price_monitor.enable_emergency_close = False
    cfg.price_monitor.emergency_close_pct = 0.05
    cfg.price_monitor.alert_threshold_pct = 0.02
    cfg.price_monitor.alert_step_pct = 0.01
    cfg.notifier.notify_on_price_alert = False
    cfg.notifier.notify_on_order_close = False
    cfg.notifier.enabled = False
    cfg.trading.remote_sl_sync_enabled = False
    cfg.trading.profit_protection_enabled = True
    cfg.trading.protect_half_r = 0.3
    cfg.trading.protect_breakeven_r = 0.5
    cfg.trading.protect_lock_r = 1.0
    cfg.trading.giveback_close_r = 0.4
    cfg.trading.giveback_close_min_mfe_r = 0.8
    return cfg


def _current_price(price: float, source: str = "mt5"):
    cp = MagicMock()
    cp.price = price
    cp.source = source
    cp.timestamp = db_now()
    return cp


def _manager_with_order(tmp_state_store, order: Order) -> PositionManager:
    pm = PositionManager(tmp_state_store)
    pm.open_position(order)
    return pm


@pytest.mark.asyncio
async def test_monitor_updates_mfe_state_without_sl_action(tmp_state_store) -> None:
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config()
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    pm = _manager_with_order(tmp_state_store, order)
    pp = MagicMock()
    pp.get_current_price.return_value = _current_price(160.2)
    broker = MagicMock()

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier", return_value=AsyncMock()):
        await monitor_open_positions(cfg, pm, pp, broker)

    updated = pm.get_open_position("USDJPY=X")
    assert updated is not None
    assert updated.max_favorable_price == pytest.approx(160.2)
    assert updated.max_favorable_r == pytest.approx(0.2)
    assert updated.stop_loss == pytest.approx(159.0)


@pytest.mark.asyncio
async def test_monitor_profit_protection_updates_internal_sl_when_remote_disabled(tmp_state_store) -> None:
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="paper")
    cfg.trading.remote_sl_sync_enabled = False
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    pm = _manager_with_order(tmp_state_store, order)
    pp = MagicMock()
    pp.get_current_price.return_value = _current_price(160.3)
    broker = MagicMock()

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier", return_value=AsyncMock()):
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.update_remote_sl.assert_not_called()
    updated = pm.get_open_position("USDJPY=X")
    assert updated is not None
    assert updated.stop_loss == pytest.approx(159.5)
    assert updated.last_protection_stage == "half"


@pytest.mark.asyncio
async def test_monitor_profit_protection_remote_first_success(tmp_state_store) -> None:
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    cfg.trading.remote_sl_sync_enabled = True
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    pm = _manager_with_order(tmp_state_store, order)
    pp = MagicMock()
    pp.get_current_price.return_value = _current_price(160.5)
    broker = MagicMock()
    broker.update_remote_sl.return_value = True

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier", return_value=AsyncMock()):
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.update_remote_sl.assert_called_once_with(order.order_id, 160.0)
    updated = pm.get_open_position("USDJPY=X")
    assert updated is not None
    assert updated.stop_loss == pytest.approx(160.0)
    assert updated.last_protection_stage == "breakeven"


@pytest.mark.asyncio
async def test_monitor_profit_protection_remote_failure_keeps_internal_sl(tmp_state_store) -> None:
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    cfg.trading.remote_sl_sync_enabled = True
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    pm = _manager_with_order(tmp_state_store, order)
    pp = MagicMock()
    pp.get_current_price.return_value = _current_price(160.5)
    broker = MagicMock()
    broker.update_remote_sl.return_value = False

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier", return_value=AsyncMock()):
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.update_remote_sl.assert_called_once_with(order.order_id, 160.0)
    updated = pm.get_open_position("USDJPY=X")
    assert updated is not None
    assert updated.stop_loss == pytest.approx(159.0)


@pytest.mark.asyncio
async def test_monitor_applies_and_clears_pending_protection_target(tmp_state_store) -> None:
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="paper")
    order = Order.new("USDJPY=X", "buy", 160.0, 159.0, 162.0, 1000.0)
    order.pending_protection_sl = 160.0
    order.pending_protection_reason = "reversal_guard"
    order.pending_protection_updated_at = db_now()
    pm = _manager_with_order(tmp_state_store, order)
    pp = MagicMock()
    pp.get_current_price.return_value = _current_price(160.1)
    broker = MagicMock()

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier", return_value=AsyncMock()):
        await monitor_open_positions(cfg, pm, pp, broker)

    updated = pm.get_open_position("USDJPY=X")
    assert updated is not None
    assert updated.stop_loss == pytest.approx(160.0)
    assert updated.pending_protection_sl is None
    assert updated.pending_protection_reason == ""
    assert updated.pending_protection_updated_at is None
