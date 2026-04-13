"""SignalBrokerAdapter のテスト。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.notifications.notifier import NullNotifier
from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order, PositionManager


def _make_signal(pair: str = "USDJPY=X", action: str = "buy") -> TradeSignal:
    news = NewsSentiment(pair=pair, sentiment_score=0.3, confidence=0.7)
    price = PriceAnalysis(
        pair=pair, direction_bias="long", bias_score=0.3, confidence=0.7,
        entry_zone=(149.5, 150.5), stop_loss=149.0, take_profit=152.0,
        risk_reward_ratio=2.0, reasoning_summary="test", analyzed_at=datetime.now(),
    )
    return TradeSignal(
        pair=pair, action=action, predicted_direction="bullish",
        combined_score=0.32, confidence=0.75,
        entry_price=150.2, stop_loss=149.8, take_profit=151.2,
        position_size=5000, signal_reason="test", detail_reason="test detail",
        news=news, price=price, generated_at=datetime.now(),
    )


@pytest.fixture
def manual_mgr(tmp_path):
    from src.persistence.state_store import StateStore
    store = StateStore(tmp_path / "manual")
    return PositionManager(store, 500000.0, context="Manual")


@pytest.fixture
def internal_mgr(tmp_path):
    from src.persistence.state_store import StateStore
    store = StateStore(tmp_path / "internal")
    return PositionManager(store, 500000.0, context="Internal")


@pytest.fixture
def broker(manual_mgr):
    from src.trading.signal_broker import SignalBrokerAdapter
    notifier = AsyncMock(spec=NullNotifier)
    return SignalBrokerAdapter(
        manual_position_mgr=manual_mgr,
        notifier=notifier,
    )


def test_execute_signal_returns_none(broker, internal_mgr):
    """execute_signal は常に None を返す（注文を発行しない）。"""
    signal = _make_signal()
    result = broker.execute_signal(signal, internal_mgr)
    assert result is None


def test_execute_signal_calls_notify(broker, internal_mgr):
    """execute_signal がシグナル推奨通知を発火する。"""
    signal = _make_signal()
    broker.execute_signal(signal, internal_mgr)
    broker._notifier.notify_signal_recommendation.assert_called_once()


def test_execute_signal_hold_skips_notification(broker, internal_mgr):
    """HOLD シグナルでは推奨通知を発火しない。"""
    signal = _make_signal(action="hold")
    broker.execute_signal(signal, internal_mgr)
    broker._notifier.notify_signal_recommendation.assert_not_called()


def test_check_and_close_returns_empty(broker, manual_mgr):
    """check_and_close_positions は常に空リストを返す。"""
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual_mgr.open_position(order)
    result = broker.check_and_close_positions([], {"USDJPY=X": 149.0}, manual_mgr)
    assert result == []


def test_check_and_close_notifies_sltp(broker, manual_mgr):
    """SL 到達で SLTPAlertEvent を発火する。"""
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual_mgr.open_position(order)
    broker.check_and_close_positions([], {"USDJPY=X": 148.5}, manual_mgr)
    broker._notifier.notify_sltp_alert.assert_called_once()


def test_check_and_close_no_alert_when_in_range(broker, manual_mgr):
    """SL/TP 未到達では通知しない。"""
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual_mgr.open_position(order)
    broker.check_and_close_positions([], {"USDJPY=X": 150.5}, manual_mgr)
    broker._notifier.notify_sltp_alert.assert_not_called()


def test_portfolio_warning_in_notification(broker, manual_mgr, internal_mgr):
    """manual に既存ポジがあるとき portfolio_warning が設定される。"""
    for i in range(4):
        manual_mgr.open_position(Order.new(
            pair=f"PAIR{i}=X", direction="buy", entry_price=150.0,
            stop_loss=149.0, take_profit=152.0, position_size=5000,
        ))
    signal = _make_signal()
    broker.execute_signal(signal, internal_mgr)
    call_args = broker._notifier.notify_signal_recommendation.call_args
    event = call_args[0][0]
    assert event.portfolio_warning != ""
