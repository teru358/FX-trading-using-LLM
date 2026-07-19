"""close 経路 (API / CLI / price_monitor) が live/MT5 時に必ず broker 経由になることを保証。

paper モードでは broker のデフォルト実装が pm.close_position に委譲するため、
本テストでは「broker.close_position が呼ばれる」のみ検証する。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _live_config(state_dir):
    cfg = MagicMock()
    cfg.mode = "live"
    cfg.live_broker = "mt5"
    cfg.state_dir = state_dir
    cfg.providers.mt5 = MagicMock(
        bridge_url="http://x:8812",
        lot_size_units=100_000,
        magic_number=12345,
        order_request_timeout_seconds=10.0,
        consecutive_reject_threshold=3,
        shadow_log_path="data/state/shadow_trades.jsonl",
        shadow_observer_state_dir="data/shadow_state",
    )
    cfg.notifier.notify_on_order_close = False
    cfg.notifier.enabled = False
    return cfg


def test_build_close_broker_paper_returns_paper_adapter(tmp_path):
    """paper モードでは PaperBrokerAdapter が返る。"""
    from src.trading.live_broker import build_close_broker
    from src.trading.paper_broker import PaperBrokerAdapter

    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.live_broker = None
    cfg.state_dir = tmp_path
    cfg.providers.mt5 = None

    broker = build_close_broker(cfg)
    assert isinstance(broker, PaperBrokerAdapter)


def test_build_close_broker_live_mt5_returns_mt5_bridge(tmp_path):
    """live + MT5 では Mt5BridgeBrokerAdapter が返る。"""
    from src.trading.live_broker import build_close_broker
    from src.trading.mt5_bridge_broker import Mt5BridgeBrokerAdapter

    cfg = _live_config(tmp_path)
    broker = build_close_broker(cfg)
    assert isinstance(broker, Mt5BridgeBrokerAdapter)


def test_api_close_route_uses_broker(tmp_path, monkeypatch):
    """/close/{pair} は close_position を broker 経由で呼ぶ。"""
    from src.api._state import state
    from src.persistence.balance_snapshot import BalanceSnapshot, write
    from datetime import datetime, timezone

    monkeypatch.setenv("API_SECRET_KEY", "test-key")

    cfg = _live_config(tmp_path)
    cfg.tradeable_instruments = []
    cfg.paper_provider = "yfinance"
    cfg.price_monitor.interval_minutes = 5

    write(tmp_path, BalanceSnapshot(
        balance=100_000.0, deposit=100_000.0, peak_balance=100_000.0,
        source="paper",
        fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    ))
    state.config = cfg
    state.analysis_store = None

    fake_broker = MagicMock()
    fake_broker.close_position.return_value = MagicMock(
        pair="USDJPY=X", direction="buy",
        entry_price=150.0, realized_pnl=10.0,
    )

    # ポジを 1 件作る
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import Order, PositionManager
    pm = PositionManager(StateStore(tmp_path), context="Test")
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=151.0, position_size=10000.0,
        signal_reason="test", macro_context_at_entry="",
        open_confidence=0.7, open_score=0.6, is_scale_in=False,
    )
    pm.open_position(order)

    # NOTE: build_close_broker は呼出側で inline import されるため、
    # 元モジュール側を patch する必要がある。
    with patch("src.trading.live_broker.build_close_broker", return_value=fake_broker), \
         patch("src.api.routes.trading.fetch_current_price",
               return_value=MagicMock(price=151.0)):
        from fastapi.testclient import TestClient
        from src.api.server import app
        client = TestClient(app)
        resp = client.post("/close/USDJPY=X", headers={"X-API-Key": "test-key"})

    assert resp.status_code == 200
    fake_broker.close_position.assert_called_once()
    call_args = fake_broker.close_position.call_args
    assert call_args.args[0] == order.order_id    # order_id
    assert call_args.args[1] == 151.0              # close_price
    assert call_args.args[2] == "manual"           # reason

    state.config = None
    state.analysis_store = None


def test_cli_close_uses_broker(tmp_path, monkeypatch):
    """CLI `_cmd_close` は broker 経由で close する。"""
    from src import cli as cli_mod
    from src.persistence.balance_snapshot import BalanceSnapshot, write
    from datetime import datetime, timezone

    cfg = _live_config(tmp_path)
    write(tmp_path, BalanceSnapshot(
        balance=100_000.0, deposit=100_000.0, peak_balance=100_000.0,
        source="paper",
        fetched_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    ))

    # ポジ 1 件 seed
    from src.persistence.state_store import StateStore
    from src.trading.position_manager import Order, PositionManager
    pm = PositionManager(StateStore(tmp_path), context="Test")
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=151.0, position_size=10000.0,
        signal_reason="test", macro_context_at_entry="",
        open_confidence=0.7, open_score=0.6, is_scale_in=False,
    )
    pm.open_position(order)

    fake_broker = MagicMock()
    fake_broker.close_position.return_value = MagicMock(
        pair="USDJPY=X", direction="buy",
        entry_price=150.0, realized_pnl=10.0,
    )

    # build_close_broker は inline import なので元モジュールを patch
    monkeypatch.setattr(
        "src.trading.live_broker.build_close_broker", lambda c: fake_broker,
    )
    monkeypatch.setattr(
        "src.cli.fetch_current_price",
        lambda pair: MagicMock(price=151.0),
    )

    cli_mod._cmd_close(cfg, "USDJPY=X")

    fake_broker.close_position.assert_called_once()


def test_price_monitor_emergency_close_uses_broker(tmp_path, monkeypatch):
    """price_monitor emergency_stop は broker 経由で close する。"""
    import asyncio
    from src.jobs.price_monitor import monitor_open_positions

    cfg = _live_config(tmp_path)
    cfg.price_monitor.enabled = True
    cfg.price_monitor.alert_threshold_pct = 0.001
    cfg.price_monitor.alert_step_pct = 0.005
    cfg.price_monitor.enable_emergency_close = True
    cfg.price_monitor.emergency_close_pct = 0.001
    cfg.trading.remote_sl_sync_enabled = False

    pm = MagicMock()
    pos = MagicMock()
    pos.order_id = "test-id"
    pos.pair = "USDJPY=X"
    pos.direction = "buy"
    pos.entry_price = 150.0
    pos.stop_loss = 149.0
    pos.take_profit = 151.0
    pos.position_size = 10000.0
    account = MagicMock(open_positions=[pos], balance=100_000.0)
    pm.get_account_state.return_value = account

    price_provider = MagicMock()
    cp = MagicMock()
    cp.price = 100.0  # 大幅 adverse move
    cp.source = "mt5"  # live モードでは MT5 source が必要
    price_provider.get_current_price.return_value = cp

    fake_broker = MagicMock()
    fake_broker.close_position.return_value = MagicMock(
        pair="USDJPY=X", direction="buy",
        entry_price=150.0, realized_pnl=-1000.0, close_reason="emergency_stop",
    )

    monkeypatch.setattr(
        "src.jobs.price_monitor.is_market_open", lambda: True,
    )

    asyncio.run(monitor_open_positions(cfg, pm, price_provider, fake_broker))
    fake_broker.close_position.assert_called_once_with(
        pos.order_id, cp.price, "emergency_stop", pm,
    )
