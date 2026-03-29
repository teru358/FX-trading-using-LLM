"""signal_combiner.combine_signals() のテスト。"""
from __future__ import annotations

import pytest

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.signals.signal_combiner import combine_signals


def _call(news: NewsSentiment, price: PriceAnalysis, pair_cfg,
          confidence_threshold: float = 0.55) -> object:
    """combine_signals の共通呼び出しヘルパー。"""
    return combine_signals(
        news=news,
        price=price,
        current_price=150.0,
        pair_cfg=pair_cfg,
        account_balance=100_000.0,
        risk_per_trade=0.01,
        confidence_threshold=confidence_threshold,
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
    """ニュースが強気・テクニカルが弱気（逆方向）→ combined_score が 50% 減衰する。"""
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
    # conflict_penalty=0.5: raw = (0.6×0.4 + (-0.6)×0.6) = -0.12, × 0.5 = -0.06
    assert abs(sig.combined_score - (-0.06)) < 0.005
    assert "[NEWS/PRICE conflict]" in sig.signal_reason


def test_sl_tp_swap_on_sell(pair_cfg):
    """SELL なのに SL < entry < TP になっている場合、自動的にスワップされる。"""
    from src.analysis.news_analyzer import NewsSentiment
    from src.analysis.price_analyzer import PriceAnalysis
    from datetime import datetime

    news = NewsSentiment(pair="USDJPY=X", sentiment_score=-0.8, confidence=0.9)
    # SL=148 < entry=150 < TP=153 → SELL なのに方向が逆（意図的に不正）
    price = PriceAnalysis(
        pair="USDJPY=X", direction_bias="short", bias_score=-0.8, confidence=0.9,
        entry_zone=(149.5, 150.5), stop_loss=148.0, take_profit=153.0,
        risk_reward_ratio=2.0, reasoning_summary="mismatch", analyzed_at=datetime.now(),
    )
    sig = _call(news, price, pair_cfg)
    assert sig.action == "sell"
    # スワップ後: SL > entry, TP < entry
    assert sig.stop_loss > sig.entry_price
    assert sig.take_profit < sig.entry_price


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
