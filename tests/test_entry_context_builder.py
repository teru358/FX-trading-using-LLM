from __future__ import annotations
import pytest
from dataclasses import dataclass, field
from src.trading.entry_context_builder import build_entry_context
from src.trading.atr_calculator import SLTPResult

def _make_news():
    @dataclass
    class FakeNews:
        sentiment_score: float = -0.25
        confidence: float = 0.70
        key_themes: list = field(default_factory=lambda: ["ECB rate decision"])
        bullish_factors: list = field(default_factory=lambda: ["strong employment"])
        bearish_factors: list = field(default_factory=lambda: ["dovish guidance"])
        summary: str = "ECBの利下げ示唆"
    return FakeNews()

def _make_price():
    @dataclass
    class FakePrice:
        direction_bias: str = "short"
        bias_score: float = -0.50
        confidence: float = 0.80
        entry_zone: tuple = (1.1530, 1.1540)
        reasoning_summary: str = "SMA20<SMA50, MACD bearish"
    return FakePrice()

def _make_sltp():
    return SLTPResult(computed_sl=1.1575, computed_tp=1.1350, llm_sl=1.1535, llm_tp=1.1490,
        adopted="computed", atr_value=0.0050, sl_atr_mult=1.5, tp_atr_mult=3.0,
        key_support=1.1480, key_resistance=1.1560)

def test_build_entry_context_contains_all_sections():
    text = build_entry_context(combined_score=-0.373, confidence=0.75, action="sell",
        news_weight=0.40, price_weight=0.60, news=_make_news(), price=_make_price(),
        sltp=_make_sltp(), macro_context="Nikkei short")
    for section in ["Signal Summary", "News Sentiment", "Technical Analysis", "SL/TP Decision", "Macro Context"]:
        assert f"=== {section} ===" in text

def test_build_entry_context_without_macro():
    text = build_entry_context(combined_score=0.30, confidence=0.70, action="buy",
        news_weight=0.40, price_weight=0.60, news=_make_news(), price=_make_price(),
        sltp=_make_sltp(), macro_context="")
    assert "=== Macro Context ===" not in text
