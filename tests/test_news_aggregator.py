"""news_aggregator.aggregate_news_sentiment() のテスト。"""
from __future__ import annotations

import pytest

from src.analysis.news_aggregator import aggregate_news_sentiment
from src.config import AppConfig, InstrumentConfig, RagConfig


class _FakeVectorStore:
    """get_recent_category_news の戻り値を差し替えるスタブ。"""

    def __init__(self, entries: list[dict]):
        self._entries = entries

    def get_recent_category_news(self, categories, lookback_hours=24):
        return [e for e in self._entries if e["metadata"].get("category") in categories]


def _make_entry(category: str, score: float, confidence: float) -> dict:
    return {
        "text": f"{category} analysis",
        "metadata": {
            "category": category,
            "sentiment_score": score,
            "confidence": confidence,
            "key_themes": "theme1, theme2",
            "summary": f"{category} summary",
            "collected_ts": 1000.0,
        },
    }


def _pair_cfg(quote_currency: str = "JPY") -> InstrumentConfig:
    return InstrumentConfig(
        symbol="USDJPY=X",
        display_name="USD/JPY",
        asset_type="fx",
        mode="trade",
        base_currency="USD",
        quote_currency=quote_currency,
    )


def _config() -> AppConfig:
    """テスト用の最小 AppConfig (rag.news_lookback_hours のみ必要)。"""
    from src.config.schema import AppConfig as _App
    from unittest.mock import MagicMock
    cfg = MagicMock(spec=_App)
    cfg.rag = MagicMock(spec=RagConfig)
    cfg.rag.news_lookback_hours = 24
    return cfg


# ── confidence 加重平均 ──────────────────────────────────────


def test_confidence_weighted_average():
    """confidence が高いカテゴリのスコアがより強く反映される。"""
    entries = [
        _make_entry("fx", score=0.8, confidence=0.9),      # 高信頼
        _make_entry("global", score=-0.2, confidence=0.1),  # 低信頼
    ]
    store = _FakeVectorStore(entries)
    pair = _pair_cfg(quote_currency="USD")  # JPY以外→japan反転なし

    result = aggregate_news_sentiment(pair, store, _config())

    # 加重平均: (0.8*0.9 + (-0.2)*0.1) / (0.9+0.1) = (0.72-0.02)/1.0 = 0.70
    assert abs(result.sentiment_score - 0.70) < 0.01


def test_simple_average_when_equal_confidence():
    """confidence が同じ場合は単純平均と一致する。"""
    entries = [
        _make_entry("fx", score=0.6, confidence=0.5),
        _make_entry("global", score=-0.4, confidence=0.5),
    ]
    store = _FakeVectorStore(entries)
    pair = _pair_cfg(quote_currency="USD")

    result = aggregate_news_sentiment(pair, store, _config())

    # (0.6*0.5 + (-0.4)*0.5) / (0.5+0.5) = 0.1
    assert abs(result.sentiment_score - 0.10) < 0.01


# ── JPY 反転 ──────────────────────────────────────────────


def test_japan_score_inverted_for_jpy_quote():
    """JPYがquoteのペアではjapanカテゴリのスコアが反転する。"""
    entries = [
        _make_entry("fx", score=0.0, confidence=0.5),
        _make_entry("japan", score=0.6, confidence=0.5),  # bullish JPY
    ]
    store = _FakeVectorStore(entries)
    pair = _pair_cfg(quote_currency="JPY")

    result = aggregate_news_sentiment(pair, store, _config())

    # japan score inverted: -0.6
    # 加重平均: (0.0*0.5 + (-0.6)*0.5) / (0.5+0.5) = -0.30
    assert result.sentiment_score < 0


def test_japan_score_not_inverted_for_non_jpy():
    """JPY以外のquoteではjapanカテゴリがそもそも対象外。
    non-JPYペアは fx+global のみ使用するため、japanエントリは含まれない。"""
    entries = [
        _make_entry("fx", score=0.4, confidence=0.5),
        _make_entry("global", score=0.6, confidence=0.5),
    ]
    store = _FakeVectorStore(entries)
    pair = _pair_cfg(quote_currency="USD")

    result = aggregate_news_sentiment(pair, store, _config())

    # 加重平均: (0.4*0.5 + 0.6*0.5) / (0.5+0.5) = 0.50
    assert abs(result.sentiment_score - 0.50) < 0.01


# ── 空データ ──────────────────────────────────────────────


def test_no_entries_returns_neutral():
    """RAGにデータがない場合はニュートラルを返す。"""
    store = _FakeVectorStore([])
    pair = _pair_cfg()

    result = aggregate_news_sentiment(pair, store, _config())

    assert result.sentiment_score == 0.0
    assert result.confidence == 0.3


# ── confidence 上限 ──────────────────────────────────────


def test_confidence_capped_at_0_9():
    """集約後の confidence は 0.9 を超えない。"""
    entries = [
        _make_entry("fx", score=0.5, confidence=0.95),
        _make_entry("global", score=0.5, confidence=0.95),
    ]
    store = _FakeVectorStore(entries)
    pair = _pair_cfg(quote_currency="USD")

    result = aggregate_news_sentiment(pair, store, _config())

    assert result.confidence <= 0.9
