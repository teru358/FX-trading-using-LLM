"""signal_combiner (combine_signals / TradeSignal) のテスト。"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.signals.signal_combiner import TradeSignal, combine_signals


def _call(news: NewsSentiment, price: PriceAnalysis, pair_cfg,
          confidence_threshold: float = 0.55,
          min_rr_ratio: float = 0.0) -> object:
    """combine_signals の共通呼び出しヘルパー。"""
    return combine_signals(
        news=news,
        price=price,
        current_price=150.0,
        pair_cfg=pair_cfg,
        account_balance=100_000.0,
        risk_per_trade=0.01,
        confidence_threshold=confidence_threshold,
        min_rr_ratio=min_rr_ratio,
    )


def test_buy_signal(bullish_news, bullish_price, pair_cfg):
    """ニュース・テクニカルともに強気 → BUY シグナル。"""
    sig = _call(bullish_news, bullish_price, pair_cfg)
    assert sig.action == "buy"


def test_sell_signal(bearish_news, bearish_price, pair_cfg):
    """ニュース・テクニカルともに弱気 → SELL シグナル。"""
    sig = _call(bearish_news, bearish_price, pair_cfg)
    assert sig.action == "sell"


def test_hold_score_too_small(neutral_news, pair_cfg):
    """合成スコアがデッドバンド(±0.15)内 → HOLD。"""
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime
    weak_price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="long", bias_score=0.1, confidence=0.8,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="weak", analyzed_at=datetime.now(),
    )
    # news=0.05×0.4 + price=0.1×0.6 = 0.02+0.06 = 0.08 → deadband
    sig = _call(neutral_news, weak_price, pair_cfg)
    assert sig.action == "hold"


def test_hold_confidence_too_low(bullish_news, bullish_price, pair_cfg):
    """スコアは閾値を超えていても confidence が低い → HOLD。"""
    # confidence_threshold を 0.99 に設定して必ず HOLD になるようにする
    sig = _call(bullish_news, bullish_price, pair_cfg, confidence_threshold=0.99)
    assert sig.combined_score > 0.15  # score alone would trigger buy
    assert sig.action == "hold"


def test_conflict_penalty(pair_cfg):
    """ニュースが強気・テクニカルが弱気（逆方向）→ conflict_penalty で減衰し、HOLD。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime

    news = NewsSentiment(pair="USDJPY=X", sentiment_score=0.6, confidence=0.8)
    price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="short", bias_score=-0.6, confidence=0.8,
        entry_zone=(149.5, 150.5), stop_loss=152.0, take_profit=147.0,
        risk_reward_ratio=2.0, reasoning_summary="conflict", analyzed_at=datetime.now(),
    )
    sig = _call(news, price, pair_cfg)
    # Dynamic weight: news.conf=0.8 → news_w=0.50, price_w=0.50
    # raw = 0.6×0.50 + (-0.6)×0.50 = 0.0
    # conflict_penalty = 1.0 - 0.5×min(0.8,0.8) = 0.60
    # combined = 0.0 × 0.60 = 0.0
    assert abs(sig.combined_score) < 0.01
    assert sig.action == "hold"
    assert "[NEWS/PRICE conflict]" in sig.signal_reason


def test_score_weighting(pair_cfg):
    """news×0.40 + price×0.60 の加重計算が正しい。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime

    news = NewsSentiment(pair="USDJPY=X", sentiment_score=0.5, confidence=0.9)
    price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="long", bias_score=0.5, confidence=0.9,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="weight test", analyzed_at=datetime.now(),
    )
    sig = _call(news, price, pair_cfg)
    expected = 0.5 * 0.40 + 0.5 * 0.60  # = 0.5
    assert abs(sig.combined_score - expected) < 0.001


# ── predicted_direction 境界値 ────────────────────────────


def test_predicted_direction_bullish_boundary(pair_cfg):
    """combined_score > 0.05 → predicted_direction = 'bullish'。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime

    news = NewsSentiment(pair="USDJPY=X", sentiment_score=0.1, confidence=0.5)
    price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="long", bias_score=0.1, confidence=0.5,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="", analyzed_at=datetime.now(),
    )
    sig = _call(news, price, pair_cfg, confidence_threshold=0.0)
    # score = 0.1*0.4 + 0.1*0.6 = 0.10 > 0.05
    assert sig.predicted_direction == "bullish"


def test_predicted_direction_neutral_in_deadband(pair_cfg):
    """combined_score ∈ [-0.05, 0.05] → predicted_direction = 'neutral'。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime

    news = NewsSentiment(pair="USDJPY=X", sentiment_score=0.01, confidence=0.5)
    price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="neutral", bias_score=0.01, confidence=0.5,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="", analyzed_at=datetime.now(),
    )
    sig = _call(news, price, pair_cfg, confidence_threshold=0.0)
    # score = 0.01*0.4 + 0.01*0.6 = 0.01 → deadband
    assert sig.predicted_direction == "neutral"


# ── dynamic weight adjustment ─────────────────────────────


def test_dynamic_weight_high_news_confidence(pair_cfg):
    """news.confidence ≥ 0.80 → news_weight が 0.10 増加する。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime

    news = NewsSentiment(pair="USDJPY=X", sentiment_score=1.0, confidence=0.85)
    price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="long", bias_score=0.0, confidence=0.5,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="", analyzed_at=datetime.now(),
    )
    sig = _call(news, price, pair_cfg, confidence_threshold=0.0)
    # effective_news_weight=0.50, effective_price_weight=0.50
    # score = 1.0*0.50 + 0.0*0.50 = 0.50
    assert abs(sig.combined_score - 0.50) < 0.01


def test_dynamic_weight_low_news_confidence(pair_cfg):
    """news.confidence ≤ 0.30 → news_weight が 0.10 減少する。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime

    news = NewsSentiment(pair="USDJPY=X", sentiment_score=1.0, confidence=0.20)
    price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="long", bias_score=0.0, confidence=0.5,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="", analyzed_at=datetime.now(),
    )
    sig = _call(news, price, pair_cfg, confidence_threshold=0.0)
    # effective_news_weight=0.30, effective_price_weight=0.70
    # score = 1.0*0.30 + 0.0*0.70 = 0.30
    assert abs(sig.combined_score - 0.30) < 0.01


# ── TradeSignal フィールド ────────────────────────────────


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
