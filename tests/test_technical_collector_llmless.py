# tests/test_technical_collector_llmless.py
import inspect
from datetime import datetime

from src.jobs import technical_collector as tc


def test_collect_one_signature_has_no_llm_or_store():
    sig = inspect.signature(tc._collect_one)
    params = set(sig.parameters)
    assert "llm" not in params
    assert "store" not in params
    assert "macro_context" not in params
    assert "correlation_context" not in params


def test_collector_module_does_not_import_analyze_price_action():
    src = inspect.getsource(tc)
    assert "analyze_price_action" not in src
    assert "format_macro_context_for_prompt" not in src
    assert "compute_correlations" not in src


def test_build_snapshot_data_from_mtf_score():
    from src.signals.technical_scorer import MultiTfTechnicalScore, TechnicalScore
    ts = TechnicalScore(
        sma_score=0.3, rsi_score=-0.1, macd_score=0.5, ichimoku_score=0.2,
        bb_score=0.0, pattern_score=0.4, adx_factor=1.2,
        total_score=0.42, confidence=0.61, direction="long",
    )
    mtf = MultiTfTechnicalScore(
        tf_scores={"long": ts, "short": ts}, tf_weights={"long": 0.5, "short": 0.5},
        alignment=0.67, raw_score=0.42, total_score=0.42, confidence=0.61, direction="long",
    )
    data = tc._build_snapshot_data(
        pair="USDJPY=X", analyzed_at=datetime(2026, 7, 11, 12, 0),
        tech_score=ts, mtf_score=mtf, chart_patterns=["engulfing_bullish"],
    )
    assert data.bias_score == 0.42
    assert data.direction_bias == "long"
    assert data.mtf_alignment == 0.67
    assert data.tf_scores["long"]["direction"] == "long"
    assert data.components["adx_factor"] == 1.2
    assert data.patterns == ["engulfing_bullish"]


def test_build_snapshot_data_single_tf_nulls_mtf():
    from src.signals.technical_scorer import TechnicalScore
    ts = TechnicalScore(
        sma_score=0.0, rsi_score=0.0, macd_score=0.0, ichimoku_score=0.0,
        bb_score=0.0, pattern_score=0.0, adx_factor=1.0,
        total_score=0.1, confidence=0.5, direction="long",
    )
    data = tc._build_snapshot_data(
        pair="EURUSD=X", analyzed_at=datetime(2026, 7, 11, 12, 0),
        tech_score=ts, mtf_score=None, chart_patterns=[],
    )
    assert data.mtf_alignment is None
    assert data.tf_scores == {}
    assert data.components["sma"] == 0.0
