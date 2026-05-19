"""signal_combiner.TradeSignal に関するテスト。"""
from __future__ import annotations

from datetime import datetime

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.signals.signal_combiner import TradeSignal


def _bare_signal() -> TradeSignal:
    return TradeSignal(
        pair="USDJPY=X", action="buy", predicted_direction="bullish",
        combined_score=0.3, confidence=0.7,
        entry_price=159.0, stop_loss=158.0, take_profit=161.0, position_size=1000.0,
        signal_reason="test", detail_reason="",
        news=NewsSentiment(pair="USDJPY=X", sentiment_score=0.1, confidence=0.5),
        price=PriceAnalysis(
            pair="USDJPY=X", direction_bias="long", bias_score=0.3, confidence=0.7,
            entry_zone=(158.0, 160.0), reasoning_summary="t",
            analyzed_at=datetime(2026, 5, 19, 12, 0),
        ),
        generated_at=datetime(2026, 5, 19, 12, 0),
    )


def test_trade_signal_tv_recommendation_defaults_empty():
    """tv_recommendation を渡さなければ空文字。"""
    assert _bare_signal().tv_recommendation == ""
