# tests/test_analysis_store_write.py
import json
from datetime import datetime

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.data.analysis_store import AnalysisStore


def test_add_snapshot_persists_json_columns(tmp_path):
    store = AnalysisStore(tmp_path / "w.db")
    data = TechnicalSnapshotData(
        pair="USDJPY=X",
        analyzed_at=datetime(2026, 7, 11, 12, 0, 0),
        bias_score=0.42, confidence=0.61, direction_bias="long",
        mtf_alignment=0.67,
        tf_scores={"long": {"score": 0.55, "direction": "long"}},
        components={"sma": 0.3, "adx_factor": 1.2},
        patterns=["engulfing_bullish"],
    )
    store.add_snapshot(data)
    row = store.get_latest_ok_row("USDJPY=X")
    assert row.bias_score == 0.42
    assert row.collect_status == "ok"
    assert row.reason is None
    assert row.mtf_alignment == 0.67
    assert json.loads(row.tf_scores_json)["long"]["direction"] == "long"
    assert json.loads(row.components_json)["adx_factor"] == 1.2
    assert json.loads(row.patterns_json) == ["engulfing_bullish"]


def test_add_snapshot_single_tf_nulls_mtf(tmp_path):
    store = AnalysisStore(tmp_path / "w2.db")
    data = TechnicalSnapshotData(
        pair="EURUSD=X", analyzed_at=datetime(2026, 7, 11, 12, 0, 0),
        bias_score=0.1, confidence=0.5, direction_bias="long",
    )
    store.add_snapshot(data)
    row = store.get_latest_ok_row("EURUSD=X")
    assert row.mtf_alignment is None
    assert json.loads(row.tf_scores_json) == {}


def test_add_sentinel_writes_reason_column(tmp_path):
    store = AnalysisStore(tmp_path / "s.db")
    store.add_sentinel(symbol="X", status="stale_price", reason="latest bar 9h ago")
    row = store.get_latest_collect_row("X")
    assert row.collect_status == "stale_price"
    assert row.reason == "latest bar 9h ago"
    assert row.mtf_alignment is None
