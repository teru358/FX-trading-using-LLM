"""price_monitor emergency_close MT5-source behavior."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.utils.clock import db_now


def _make_config(mode="live"):
    cfg = MagicMock()
    cfg.mode = mode
    cfg.price_monitor.enabled = True
    cfg.price_monitor.enable_emergency_close = True
    cfg.price_monitor.emergency_close_pct = 0.05
    cfg.price_monitor.alert_threshold_pct = 0.02
    cfg.price_monitor.alert_step_pct = 0.01
    cfg.notifier.notify_on_price_alert = True
    cfg.notifier.notify_on_order_close = False
    cfg.notifier.enabled = False
    cfg.trading.remote_sl_sync_enabled = False
    cfg.trading.profit_protection_enabled = False
    return cfg


def _make_position(pair="USDJPY=X", direction="buy", entry=150.0, sl=149.0, tp=152.0):
    pos = MagicMock()
    pos.order_id = "mt5:12345"
    pos.pair = pair
    pos.direction = direction
    pos.entry_price = entry
    pos.stop_loss = sl
    pos.initial_stop_loss = sl
    pos.take_profit = tp
    pos.position_size = 1000.0
    pos.opened_at = db_now()
    pos.max_favorable_price = None
    pos.max_favorable_r = 0.0
    pos.pending_protection_sl = None
    return pos


def _make_current_price(price, source):
    cp = MagicMock()
    cp.price = price
    cp.source = source
    cp.timestamp = db_now()
    return cp


@pytest.mark.asyncio
async def test_emergency_close_skipped_when_source_not_mt5_in_live() -> None:
    """live モードで source!=mt5 のとき emergency_close は skip され DEGRADED alert のみ。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    pos = _make_position(direction="buy", entry=150.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(142.0, "yfinance")
    broker = MagicMock()

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.close_position.assert_not_called()
    notifier.notify_price_alert.assert_called()


@pytest.mark.asyncio
async def test_emergency_close_fires_when_source_mt5() -> None:
    """source=mt5 で adverse_pct 超過 → emergency_close 発火。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    pos = _make_position(direction="buy", entry=150.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(142.0, "mt5")
    broker = MagicMock()
    closed_order = MagicMock()
    closed_order.pair = pos.pair
    closed_order.direction = pos.direction
    closed_order.entry_price = pos.entry_price
    closed_order.realized_pnl = -100.0
    broker.close_position.return_value = closed_order

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.close_position.assert_called_once()


@pytest.mark.asyncio
async def test_emergency_close_unchanged_in_paper_mode() -> None:
    """paper モードでは source 不問で emergency_close 発火。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="paper")
    pos = _make_position(direction="buy", entry=150.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(142.0, "yfinance")
    broker = MagicMock()
    broker.close_position.return_value = MagicMock()

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.close_position.assert_called_once()


@pytest.mark.asyncio
async def test_emergency_close_skipped_when_source_not_mt5_in_live_test() -> None:
    """live_test モードでも source!=mt5 のとき emergency_close は skip される。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live_test")
    pos = _make_position(direction="buy", entry=150.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(142.0, "yfinance")
    broker = MagicMock()

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.close_position.assert_not_called()
    notifier.notify_price_alert.assert_called()
