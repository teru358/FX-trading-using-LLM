"""signal モード用通知イベントのテスト。"""
from __future__ import annotations

import pytest

from src.notifications.notifier import (
    NullNotifier,
    ReviewAdvisoryEvent,
    SignalRecommendationEvent,
    SLTPAlertEvent,
)


@pytest.mark.asyncio
async def test_signal_recommendation_notify():
    """SignalRecommendationEvent が NullNotifier で例外なく処理される。"""
    notifier = NullNotifier()
    event = SignalRecommendationEvent(
        pair="USDJPY=X", direction="buy", entry_price=150.200,
        stop_loss=149.800, take_profit=151.200, position_size=5000,
        combined_score=0.32, confidence=0.75, signal_reason="score=+0.32",
        detail_reason="tech +0.20 news +0.12", max_loss=10000.0,
        portfolio_warning="", existing_positions=0, source="trading",
    )
    await notifier.notify_signal_recommendation(event)


@pytest.mark.asyncio
async def test_signal_recommendation_with_portfolio_warning():
    """ポートフォリオ警告付きの通知が処理される。"""
    notifier = NullNotifier()
    event = SignalRecommendationEvent(
        pair="GBPJPY=X", direction="buy", entry_price=190.500,
        stop_loss=189.500, take_profit=192.500, position_size=3000,
        combined_score=0.28, confidence=0.65, signal_reason="score=+0.28",
        detail_reason="", max_loss=6000.0,
        portfolio_warning="JPY group already has 2 positions (max 2)",
        existing_positions=1, source="trading",
    )
    await notifier.notify_signal_recommendation(event)


@pytest.mark.asyncio
async def test_sltp_alert_notify():
    """SLTPAlertEvent が処理される。"""
    notifier = NullNotifier()
    event = SLTPAlertEvent(
        pair="USDJPY=X", direction="buy", order_id="test-123",
        entry_price=150.200, current_price=149.800,
        trigger="stop_loss", unrealized_pnl=-2000.0,
    )
    await notifier.notify_sltp_alert(event)


@pytest.mark.asyncio
async def test_review_advisory_notify():
    """ReviewAdvisoryEvent が処理される。"""
    notifier = NullNotifier()
    event = ReviewAdvisoryEvent(
        pair="USDJPY=X", direction="buy", order_id="test-456",
        close_reason="reversal",
        detail="Signal reversed: buy vs bearish (score=-0.30 conf=0.80)",
        current_price=149.500,
    )
    await notifier.notify_review_advisory(event)
