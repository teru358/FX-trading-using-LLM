"""共通 fixture。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.config import InstrumentConfig
from src.persistence.state_store import StateStore
from src.trading.position_manager import Order


@pytest.fixture
def tmp_state_store(tmp_path: Path) -> StateStore:
    """実ファイルを tmp_path に書く StateStore。テスト間で干渉しない。"""
    return StateStore(tmp_path)


@pytest.fixture
def pair_cfg() -> InstrumentConfig:
    """テスト用の USDJPY=X 設定。"""
    return InstrumentConfig(
        symbol="USDJPY=X",
        display_name="USD/JPY",
        asset_type="fx",
        mode="trade",
        pip_value=0.01,
        base_currency="USD",
        quote_currency="JPY",
    )


@pytest.fixture
def bullish_news() -> NewsSentiment:
    """強気ニュースセンチメント。"""
    return NewsSentiment(
        pair="USDJPY=X",
        sentiment_score=0.6,
        confidence=0.8,
    )


@pytest.fixture
def bearish_news() -> NewsSentiment:
    """弱気ニュースセンチメント。"""
    return NewsSentiment(
        pair="USDJPY=X",
        sentiment_score=-0.6,
        confidence=0.8,
    )


@pytest.fixture
def neutral_news() -> NewsSentiment:
    """中立ニュースセンチメント。"""
    return NewsSentiment(
        pair="USDJPY=X",
        sentiment_score=0.05,
        confidence=0.5,
    )


@pytest.fixture
def bullish_price() -> PriceAnalysis:
    """強気テクニカル分析。"""
    return PriceAnalysis(
        pair="USDJPY=X",
        direction_bias="long",
        bias_score=0.7,
        confidence=0.75,
        entry_zone=(149.5, 150.5),
        stop_loss=148.0,
        take_profit=153.0,
        risk_reward_ratio=2.0,
        reasoning_summary="Bullish momentum",
        analyzed_at=datetime.now(),
    )


@pytest.fixture
def bearish_price() -> PriceAnalysis:
    """弱気テクニカル分析。"""
    return PriceAnalysis(
        pair="USDJPY=X",
        direction_bias="short",
        bias_score=-0.7,
        confidence=0.75,
        entry_zone=(149.5, 150.5),
        stop_loss=152.0,
        take_profit=147.0,
        risk_reward_ratio=2.0,
        reasoning_summary="Bearish momentum",
        analyzed_at=datetime.now(),
    )


@pytest.fixture
def buy_order() -> Order:
    """テスト用の BUY オープンポジション（USDJPY=X、entry=150.0）。"""
    return Order.new(
        pair="USDJPY=X",
        direction="buy",
        entry_price=150.0,
        stop_loss=149.0,
        take_profit=152.0,
        position_size=10000.0,
        signal_reason="test",
    )


@pytest.fixture
def sell_order() -> Order:
    """テスト用の SELL オープンポジション（USDJPY=X、entry=150.0）。"""
    return Order.new(
        pair="USDJPY=X",
        direction="sell",
        entry_price=150.0,
        stop_loss=151.0,
        take_profit=148.0,
        position_size=10000.0,
        signal_reason="test",
    )
