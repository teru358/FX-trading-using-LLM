# tests/test_price_analyzer_omit.py
from dataclasses import fields

from src.analysis import price_analyzer as pa


def test_analyze_price_action_removed():
    assert not hasattr(pa, "analyze_price_action")
    assert not hasattr(pa, "load_audit_lessons")


def test_price_analysis_kept_symbols():
    assert hasattr(pa, "PriceAnalysis")
    assert hasattr(pa, "load_user_notes")


def test_price_analysis_no_key_support_resistance():
    names = {f.name for f in fields(pa.PriceAnalysis)}
    assert "key_support" not in names
    assert "key_resistance" not in names
    # keep-with-defaults fields retained
    assert "market_regime" in names
    assert "stop_loss" in names
    assert "reasoning_summary" in names
