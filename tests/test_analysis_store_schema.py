# tests/test_analysis_store_schema.py
from sqlalchemy import create_engine, text

from src.data.analysis_store import AnalysisStore


def _columns(engine, table="technical_snapshots"):
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def test_new_schema_has_json_columns_and_drops_llm_columns(tmp_path):
    db = tmp_path / "a.db"
    store = AnalysisStore(db)
    cols = _columns(store._engine)
    # 新設
    assert {"mtf_alignment", "tf_scores_json", "components_json",
            "patterns_json", "reason"} <= cols
    # 削除
    assert not ({"stop_loss", "take_profit", "entry_zone_low", "entry_zone_high",
                 "risk_reward_ratio", "reasoning_summary", "market_regime",
                 "confidence_modifier"} & cols)
    # 維持
    assert {"id", "symbol", "analyzed_at", "collect_status",
            "bias_score", "confidence", "direction_bias"} <= cols


def test_migration_drops_and_recreates_old_shape_table(tmp_path):
    # 注意: `_get_engine()` は呼ぶたびに _Base.metadata.create_all を走らせる
    # (price_store.py:58-60) ため、旧形状 DB の構築には使えない。素の create_engine で
    # migration 前状態を正確に再現する。
    db = tmp_path / "b.db"
    raw = create_engine(f"sqlite:///{db}")
    with raw.connect() as conn:
        conn.execute(text(
            "CREATE TABLE technical_snapshots ("
            "id INTEGER PRIMARY KEY, symbol TEXT, analyzed_at DATETIME, "
            "bias_score FLOAT, confidence FLOAT, direction_bias TEXT, "
            "stop_loss FLOAT, take_profit FLOAT, reasoning_summary TEXT, "
            "market_regime TEXT, confidence_modifier FLOAT, collect_status TEXT)"
        ))
        conn.execute(text("INSERT INTO technical_snapshots (id, symbol) VALUES (1, 'X')"))
        conn.commit()
    raw.dispose()
    store = AnalysisStore(db)
    cols = _columns(store._engine)
    assert "tf_scores_json" in cols
    assert "stop_loss" not in cols
    with store._engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM technical_snapshots")).scalar()
    assert n == 0


def test_migration_is_idempotent_keeps_rows(tmp_path):
    db = tmp_path / "c.db"
    store = AnalysisStore(db)
    with store._engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO technical_snapshots "
            "(symbol, analyzed_at, bias_score, confidence, direction_bias, collect_status) "
            "VALUES ('Y', '2026-07-11 12:00:00', 0.1, 0.5, 'long', 'ok')"
        ))
        conn.commit()
    AnalysisStore(db)
    with store._engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM technical_snapshots WHERE symbol='Y'")).scalar()
    assert n == 1
