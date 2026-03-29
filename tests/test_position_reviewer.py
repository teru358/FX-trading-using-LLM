"""position_reviewer.review_open_positions() のテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.signals.signal_combiner import TradeSignal
from src.trading.position_reviewer import review_open_positions


def _make_signal(pair: str, predicted_direction: str,
                 confidence: float, combined_score: float) -> TradeSignal:
    """テスト用 TradeSignal を組み立てるヘルパー。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis

    news = NewsSentiment(pair=pair, sentiment_score=combined_score, confidence=confidence)
    price = PriceAnalysis(
        pair=pair, direction_bias="long" if combined_score > 0 else "short",
        bias_score=combined_score, confidence=confidence,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="test", analyzed_at=datetime.now(),
    )
    return TradeSignal(
        pair=pair,
        action="buy" if combined_score > 0 else "sell",
        predicted_direction=predicted_direction,
        combined_score=combined_score,
        confidence=confidence,
        entry_price=150.0,
        stop_loss=148.0,
        take_profit=153.0,
        position_size=10000.0,
        signal_reason="test",
        detail_reason="test",
        news=news,
        price=price,
        generated_at=datetime.now(),
    )


def test_layer1_reversal_closes(buy_order):
    """Layer1: BUY ポジションに bearish シグナル + confidence ≥ 0.70 → 決済判断が返る。"""
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.80, combined_score=-0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_confidence_min=0.70,
        reversal_score_threshold=0.25,
    )
    assert len(decisions) == 1
    assert decisions[0].close_reason == "reversal"
    assert decisions[0].order_id == buy_order.order_id


def test_layer1_low_confidence_skips(buy_order):
    """Layer1: confidence < 0.70 → 決済判断は返らない。"""
    signal = _make_signal("USDJPY=X", "bearish", confidence=0.60, combined_score=-0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 150.5},
        reversal_confidence_min=0.70,
    )
    assert len(decisions) == 0


def test_layer2_timeout_closes(buy_order):
    """Layer2: max_holding_days 超過 + TP進捗 < 30% → timeout 決済判断が返る。"""
    # opened_at を 4日前に設定
    buy_order.opened_at = datetime.now() - timedelta(days=4)
    # 現在価格 = entry + 0.1（TP=152.0 まで 2.0 のうち 0.1 = 5% 進捗）
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 150.1},
        max_holding_days=3,
        timeout_min_progress_pct=0.30,
    )
    assert len(decisions) == 1
    assert decisions[0].close_reason == "timeout"


def test_layer2_sufficient_progress_skips(buy_order):
    """Layer2: TP進捗 ≥ 30% → タイムアウトでも決済しない。"""
    buy_order.opened_at = datetime.now() - timedelta(days=4)
    # entry=150.0, TP=152.0: distance=2.0, 進捗 0.8/2.0 = 40%
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={},
        current_prices={"USDJPY=X": 150.8},
        max_holding_days=3,
        timeout_min_progress_pct=0.30,
    )
    assert len(decisions) == 0


def test_layer3_profit_lock_closes(buy_order):
    """Layer3: TP進捗 ≥ 40% + |signal score| < 0.15 → profit_lock 決済判断が返る。"""
    # entry=150.0, TP=152.0: distance=2.0, 進捗 1.0/2.0 = 50%
    signal = _make_signal("USDJPY=X", "bullish", confidence=0.6, combined_score=0.05)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 151.0},
        profit_lock_min_progress_pct=0.40,
        profit_lock_score_floor=0.15,
    )
    assert len(decisions) == 1
    assert decisions[0].close_reason == "profit_lock"


def test_layer3_strong_signal_skips(buy_order):
    """Layer3: |signal score| ≥ 0.15 → 利益ロックはしない。"""
    signal = _make_signal("USDJPY=X", "bullish", confidence=0.8, combined_score=0.30)
    decisions = review_open_positions(
        open_positions=[buy_order],
        signals_by_pair={"USDJPY=X": signal},
        current_prices={"USDJPY=X": 151.0},
        profit_lock_min_progress_pct=0.40,
        profit_lock_score_floor=0.15,
    )
    assert len(decisions) == 0
