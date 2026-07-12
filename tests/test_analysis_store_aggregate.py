# tests/test_analysis_store_aggregate.py
from datetime import timedelta

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.data.analysis_store import AnalysisStore
from src.utils.clock import db_now


def test_aggregate_returns_defaults_for_removed_fields(tmp_path):
    store = AnalysisStore(tmp_path / "agg.db")
    now = db_now()
    for k in range(3):
        store.add_snapshot(TechnicalSnapshotData(
            pair="USDJPY=X",
            analyzed_at=now - timedelta(minutes=15 * k),
            bias_score=0.4, confidence=0.6, direction_bias="long",
        ))
    price = store.aggregate("USDJPY=X", hours=8)
    assert price is not None
    assert price.direction_bias == "long"
    assert price.bias_score > 0
    # 削除列は既定値
    assert price.stop_loss == 0.0
    assert price.take_profit == 0.0
    assert price.entry_zone == (0.0, 0.0)
    assert price.risk_reward_ratio == 2.0
    assert price.market_regime == "unknown"
    assert price.confidence_modifier == 0.0


def test_aggregate_none_when_empty(tmp_path):
    store = AnalysisStore(tmp_path / "agg2.db")
    assert store.aggregate("NONE=X", hours=8) is None
