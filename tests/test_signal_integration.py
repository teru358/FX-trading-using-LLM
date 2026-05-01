"""signal モードの統合テスト。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.notifications.notifier import NullNotifier
from src.persistence.state_store import StateStore
from src.trading.position_manager import Order, PositionManager


@pytest.fixture
def dual_mgrs(tmp_path):
    internal_store = StateStore(tmp_path / "internal")
    manual_store = StateStore(tmp_path / "manual")
    internal = PositionManager(internal_store, 500000.0, context="Internal")
    manual = PositionManager(manual_store, 500000.0, context="Manual")
    return internal, manual


def test_dual_managers_independent(dual_mgrs):
    """internal と manual の PositionManager が独立動作する。"""
    internal, manual = dual_mgrs
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    internal.open_position(order)
    assert len(internal.get_account_state().open_positions) == 1
    assert len(manual.get_account_state().open_positions) == 0


def test_manual_close_triggers_balance_update(dual_mgrs):
    """manual close で残高が自動更新される。"""
    _, manual = dual_mgrs
    order = Order.new(
        pair="USDJPY=X", direction="buy", entry_price=150.0,
        stop_loss=149.0, take_profit=152.0, position_size=5000,
    )
    manual.open_position(order)
    manual.close_position(order.order_id, 151.0, "take_profit")
    account = manual.get_account_state()
    assert account.balance > 500000.0
    assert len(account.open_positions) == 0


def test_signal_broker_uses_manual_for_guard(dual_mgrs):
    """SignalBrokerAdapter が manual ポジションを参照してガード判定する。

    max_positions_per_pair=1 のとき、manual に同ペアのポジションが 1 件あれば
    execute_signal はスキップし、notify_signal_recommendation を呼ばない。
    """
    internal, manual = dual_mgrs
    from src.trading.signal_broker import SignalBrokerAdapter

    notifier = AsyncMock(spec=NullNotifier)
    broker = SignalBrokerAdapter(
        manual_position_mgr=manual, notifier=notifier,
        max_positions_per_pair=1,
    )
    manual.open_position(Order.new(
        pair="EURUSD=X", direction="buy", entry_price=1.08,
        stop_loss=1.07, take_profit=1.11, position_size=5000,
    ))

    # _make_signal ヘルパーを直接定義（外部テストに依存しない）
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from src.signals.signal_combiner import TradeSignal
    news = NewsSentiment(pair="EURUSD=X", sentiment_score=0.3, confidence=0.7)
    price = PriceAnalysis(
        pair="EURUSD=X", direction_bias="long", bias_score=0.3, confidence=0.7,
        entry_zone=(1.08, 1.09), stop_loss=1.07, take_profit=1.11,
        risk_reward_ratio=2.0, reasoning_summary="test", analyzed_at=datetime.now(),
    )
    signal = TradeSignal(
        pair="EURUSD=X", action="buy", predicted_direction="bullish",
        combined_score=0.32, confidence=0.75,
        entry_price=1.085, stop_loss=1.075, take_profit=1.105,
        position_size=5000, signal_reason="test", detail_reason="test",
        news=news, price=price, generated_at=datetime.now(),
    )

    result = broker.execute_signal(signal, internal)
    # ペア上限に達しているので通知されずスキップされる
    assert result is None
    notifier.notify_signal_recommendation.assert_not_called()
