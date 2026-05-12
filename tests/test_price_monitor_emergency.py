"""price_monitor の emergency_close MT5-source 判定 + remote SL sync テスト。"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.utils.clock import db_now


def _make_config(mode="live"):
    """テスト用の最小 AppConfig stub。既存 test_price_monitor_trailing.py の値に揃える。"""
    cfg = MagicMock()
    cfg.mode = mode
    cfg.price_monitor.enabled = True
    cfg.price_monitor.trailing_stop_enabled = True
    cfg.price_monitor.enable_emergency_close = True
    cfg.price_monitor.emergency_close_pct = 0.05
    cfg.price_monitor.alert_threshold_pct = 0.02
    cfg.price_monitor.alert_step_pct = 0.01
    cfg.price_monitor.trailing_stop_breakeven_pct = 0.20  # half=0.10 で 10% 進捗から発火
    cfg.price_monitor.trailing_stop_activation_pct = 0.40
    cfg.price_monitor.trailing_stop_distance_ratio = 1.0
    cfg.notifier.notify_on_price_alert = True
    cfg.notifier.notify_on_order_close = False
    cfg.notifier.enabled = False
    cfg.trading.remote_sl_sync_enabled = False  # default
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
    pp.get_current_price.return_value = _make_current_price(142.0, "yfinance")  # 5.3% adverse
    broker = MagicMock()
    broker.update_remote_sl.return_value = True

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.close_position.assert_not_called()
    notifier.notify_price_alert.assert_called()


@pytest.mark.asyncio
async def test_emergency_close_fires_when_source_mt5() -> None:
    """source=mt5 で adverse_pct 超過 → 既存通り emergency_close 発火。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    pos = _make_position(direction="buy", entry=150.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(142.0, "mt5")
    broker = MagicMock()
    broker.update_remote_sl.return_value = True
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
    """paper モードでは source 不問で emergency_close 発火 (既存挙動維持)。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="paper")
    pos = _make_position(direction="buy", entry=150.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(142.0, "yfinance")
    broker = MagicMock()
    broker.update_remote_sl.return_value = True
    closed_order = MagicMock()
    broker.close_position.return_value = closed_order

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.close_position.assert_called_once()


@pytest.mark.asyncio
async def test_remote_sl_not_pushed_when_flag_disabled() -> None:
    """remote_sl_sync_enabled=False (default) では update_remote_sl は呼ばれない。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    cfg.trading.remote_sl_sync_enabled = False
    pos = _make_position(direction="buy", entry=150.0, sl=149.0, tp=160.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(151.0, "mt5")  # half stage
    broker = MagicMock()
    broker.update_remote_sl.return_value = True

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.update_remote_sl.assert_not_called()


@pytest.mark.asyncio
async def test_remote_sl_pushed_with_target_sl_when_enabled() -> None:
    """flag=True で trailing 発火時、broker に target_sl (計算値) が渡る。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    cfg.trading.remote_sl_sync_enabled = True
    # breakeven_pct=0.20 で entry=150, sl=149 (distance=1) → half(0.10) で midpoint=149.5
    pos = _make_position(direction="buy", entry=150.0, sl=149.0, tp=160.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pm.update_stop_loss.return_value = True
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(151.0, "mt5")  # progress=10%
    broker = MagicMock()
    broker.update_remote_sl.return_value = True

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.update_remote_sl.assert_called_once()
    _, target_sl = broker.update_remote_sl.call_args.args
    assert target_sl == pytest.approx(149.5, abs=0.01)
    pm.update_stop_loss.assert_called_once()


@pytest.mark.asyncio
async def test_internal_sl_not_advanced_when_remote_fails() -> None:
    """remote push 失敗時、内部 SL は据え置き (= update_stop_loss 呼ばれない)。"""
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _make_config(mode="live")
    cfg.trading.remote_sl_sync_enabled = True
    pos = _make_position(direction="buy", entry=150.0, sl=149.0, tp=160.0)
    pm = MagicMock()
    pm.get_account_state.return_value.open_positions = [pos]
    pp = MagicMock()
    pp.get_current_price.return_value = _make_current_price(151.0, "mt5")
    broker = MagicMock()
    broker.update_remote_sl.return_value = False  # remote 失敗

    with patch("src.jobs.price_monitor.is_market_open", return_value=True), \
         patch("src.jobs.price_monitor.create_notifier") as mock_create:
        notifier = AsyncMock()
        mock_create.return_value = notifier
        await monitor_open_positions(cfg, pm, pp, broker)

    broker.update_remote_sl.assert_called_once()
    pm.update_stop_loss.assert_not_called()
