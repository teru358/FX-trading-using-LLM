# tests/test_technical_snapshot_data.py
from datetime import datetime

from src.analysis.technical_snapshot_data import TechnicalSnapshotData


def test_snapshot_data_holds_deterministic_fields():
    data = TechnicalSnapshotData(
        pair="USDJPY=X",
        analyzed_at=datetime(2026, 7, 11, 12, 0, 0),
        bias_score=0.42,
        confidence=0.61,
        direction_bias="long",
        mtf_alignment=0.67,
        tf_scores={"long": {"score": 0.55, "direction": "long"}},
        components={"sma": 0.3, "rsi": -0.1, "adx_factor": 1.2},
        patterns=["engulfing_bullish"],
    )
    assert data.pair == "USDJPY=X"
    assert data.mtf_alignment == 0.67
    assert data.tf_scores["long"]["direction"] == "long"
    assert data.patterns == ["engulfing_bullish"]


def test_snapshot_data_mtf_optional():
    # 単一 TF fallback: mtf_alignment None / tf_scores 空
    data = TechnicalSnapshotData(
        pair="EURUSD=X",
        analyzed_at=datetime(2026, 7, 11, 12, 0, 0),
        bias_score=0.1,
        confidence=0.5,
        direction_bias="long",
    )
    assert data.mtf_alignment is None
    assert data.tf_scores == {}
    assert data.components == {}
    assert data.patterns == []
