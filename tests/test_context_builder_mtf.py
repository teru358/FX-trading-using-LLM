# tests/test_context_builder_mtf.py
from src.analysis.technical_snapshot_data import TechnicalSnapshotData
from src.data.analysis_store import AnalysisStore
from src.utils.clock import db_now


def _row(store, symbol):
    return store.get_latest_ok_row(symbol)


def test_ok_context_includes_mtf_patterns_components(tmp_path):
    from src.orchestrator.context_builder import _format_technical_ok
    store = AnalysisStore(tmp_path / "c.db")
    store.add_snapshot(TechnicalSnapshotData(
        pair="USDJPY=X", analyzed_at=db_now(),
        bias_score=0.42, confidence=0.61, direction_bias="long",
        mtf_alignment=0.67,
        tf_scores={"long": {"score": 0.55, "direction": "long"}},
        components={"sma": 0.3, "adx_factor": 1.2},
        patterns=["engulfing_bullish"],
    ))
    row = _row(store, "USDJPY=X")
    ctx = _format_technical_ok(row, now=db_now())
    assert ctx["status"] == "ok"
    assert ctx["mtf"]["alignment"] == 0.67
    assert ctx["mtf"]["long"]["dir"] == "long"
    assert ctx["patterns"] == ["engulfing_bullish"]
    assert ctx["components"]["adx_factor"] == 1.2
    # core fields retained
    assert ctx["bias_score"] == 0.42
    assert ctx["direction"] == "long"
    assert ctx["_ref"]["snapshot_id"] == row.id


def test_single_tf_row_yields_null_mtf(tmp_path):
    from src.orchestrator.context_builder import _format_technical_ok
    store = AnalysisStore(tmp_path / "c2.db")
    store.add_snapshot(TechnicalSnapshotData(
        pair="EURUSD=X", analyzed_at=db_now(),
        bias_score=0.1, confidence=0.5, direction_bias="long",
    ))
    row = _row(store, "EURUSD=X")
    ctx = _format_technical_ok(row, now=db_now())
    assert ctx["mtf"] is None
    assert ctx["patterns"] == []
    assert ctx["components"] == {}


def test_corrupt_json_columns_fail_safe(tmp_path):
    from sqlalchemy import text
    from src.orchestrator.context_builder import _format_technical_ok
    store = AnalysisStore(tmp_path / "c3.db")
    store.add_snapshot(TechnicalSnapshotData(
        pair="GBPUSD=X", analyzed_at=db_now(),
        bias_score=0.2, confidence=0.5, direction_bias="long",
        mtf_alignment=0.67, tf_scores={"long": {"score": 0.5, "direction": "long"}},
    ))
    with store._engine.connect() as conn:
        conn.execute(text(
            "UPDATE technical_snapshots SET tf_scores_json='{broken', "
            "components_json='not json', patterns_json='[1,' WHERE symbol='GBPUSD=X'"
        ))
        conn.commit()
    row = _row(store, "GBPUSD=X")
    ctx = _format_technical_ok(row, now=db_now())  # must not raise
    assert ctx["status"] == "ok"
    assert ctx["mtf"] is None        # tf_scores corrupt → mtf can't be built
    assert ctx["components"] == {}
    assert ctx["patterns"] == []


def test_inner_shape_corruption_does_not_crash(tmp_path):
    from sqlalchemy import text
    from src.orchestrator.context_builder import _format_technical_ok
    store = AnalysisStore(tmp_path / "c4.db")
    store.add_snapshot(TechnicalSnapshotData(
        pair="AUDUSD=X", analyzed_at=db_now(),
        bias_score=0.2, confidence=0.5, direction_bias="long", mtf_alignment=0.67,
    ))
    with store._engine.connect() as conn:
        conn.execute(text(
            "UPDATE technical_snapshots SET tf_scores_json='{\"long\": \"notadict\", \"short\": 5}' "
            "WHERE symbol='AUDUSD=X'"
        ))
        conn.commit()
    row = _row(store, "AUDUSD=X")
    ctx = _format_technical_ok(row, now=db_now())  # must not raise
    assert ctx["status"] == "ok"
    # 不正な inner entry はスキップされ、alignment だけの mtf になる
    assert ctx["mtf"]["alignment"] == 0.67
    assert "long" not in ctx["mtf"]  # notadict はスキップ
    assert "short" not in ctx["mtf"]  # 5 (非dict) はスキップ
