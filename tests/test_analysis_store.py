"""AnalysisStore.aggregate() のテスト。

方向閾値・時間加重・SL/TP 選択ロジックを検証する。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from src.analysis.price_analyzer import PriceAnalysis
from src.data.analysis_store import AnalysisStore
from src.utils.clock import db_now


@pytest.fixture
def store(tmp_path: Path) -> AnalysisStore:
    return AnalysisStore(tmp_path / "test.db")


def _snapshot(
    symbol: str = "USDJPY=X",
    direction: str = "long",
    bias: float = 0.3,
    confidence: float = 0.7,
    hours_ago: float = 0,
    sl: float = 149.0,
    tp: float = 152.0,
) -> PriceAnalysis:
    return PriceAnalysis(
        pair=symbol,
        direction_bias=direction,
        bias_score=bias,
        confidence=confidence,
        entry_zone=(149.5, 150.5),
        stop_loss=sl,
        take_profit=tp,
        risk_reward_ratio=2.0,
        reasoning_summary=f"test {direction}",
        analyzed_at=db_now() - timedelta(hours=hours_ago),
    )


# ── direction 閾値 0.05 ──────────────────────────────────


def test_aggregate_direction_threshold_long(store: AnalysisStore):
    """bias > 0.05 → direction = 'long'"""
    store.add_snapshot(_snapshot(bias=0.06))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "long"


def test_get_latest_snapshot_returns_most_recent_even_outside_lookback(
    store: AnalysisStore,
):
    """表示用途では lookback 外でも保存済み最新 snapshot を取得できる。"""
    store.add_snapshot(_snapshot(bias=0.1, hours_ago=24))
    store.add_snapshot(_snapshot(bias=0.4, hours_ago=12))

    latest = store.get_latest_snapshot("USDJPY=X")

    assert latest is not None
    assert latest.bias_score == 0.4


def test_aggregate_direction_threshold_short(store: AnalysisStore):
    """bias < -0.05 → direction = 'short'"""
    store.add_snapshot(_snapshot(direction="short", bias=-0.06))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "short"


def test_aggregate_direction_threshold_neutral(store: AnalysisStore):
    """-0.05 ≤ bias ≤ 0.05 → direction = 'neutral'"""
    store.add_snapshot(_snapshot(direction="neutral", bias=0.04))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "neutral"


def test_aggregate_direction_boundary_exactly_0_05(store: AnalysisStore):
    """bias == 0.05 は neutral (> 0.05 が long の条件)。"""
    store.add_snapshot(_snapshot(direction="neutral", bias=0.05))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "neutral"


# ── 時間加重 ──────────────────────────────────────────────


def test_aggregate_recent_weighted_more(store: AnalysisStore):
    """最近のスナップショットがより重く加重される。"""
    store.add_snapshot(_snapshot(bias=0.8, hours_ago=0))    # 最新 → weight ≈ 1.0
    store.add_snapshot(_snapshot(bias=-0.2, hours_ago=4))   # 4h前 → weight ≈ 0.2
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    # 単純平均なら 0.3 だが、時間加重で 0.8 寄りになる
    assert result.bias_score > 0.5


# ── SL/TP 選択 ────────────────────────────────────────────


def test_aggregate_sltp_from_direction_matched_snapshot(store: AnalysisStore):
    """集約方向と一致するスナップショットの SL/TP が採用される。"""
    # 最新は short だが、集約方向が long になるケース
    store.add_snapshot(_snapshot(direction="long", bias=0.6, hours_ago=0,
                                   sl=148.0, tp=153.0))
    store.add_snapshot(_snapshot(direction="short", bias=-0.1, hours_ago=2,
                                   sl=152.0, tp=147.0))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "long"
    assert result.stop_loss == 148.0
    assert result.take_profit == 153.0


# ── 空データ ──────────────────────────────────────────────


def test_aggregate_no_data_returns_none(store: AnalysisStore):
    """スナップショットがない場合は None を返す。"""
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is None


# ── confidence consistency 減衰 ──────────────────────────


def test_aggregate_confidence_reduced_by_inconsistency(store: AnalysisStore):
    """方向がばらつくと confidence が下がる（一致時に比べて）。"""
    # 一致ケース: 2つとも long
    store_consistent = store
    store_consistent.add_snapshot(_snapshot(direction="long", bias=0.3, confidence=0.8, hours_ago=0))
    store_consistent.add_snapshot(_snapshot(direction="long", bias=0.3, confidence=0.8, hours_ago=1))
    consistent = store_consistent.aggregate("USDJPY=X", hours=8)

    # 不一致ケース用に別シンボルで
    store.add_snapshot(_snapshot(symbol="EURUSD=X", direction="long", bias=0.3, confidence=0.8, hours_ago=0))
    store.add_snapshot(_snapshot(symbol="EURUSD=X", direction="short", bias=-0.3, confidence=0.8, hours_ago=1))
    inconsistent = store.aggregate("EURUSD=X", hours=8)

    assert consistent is not None
    assert inconsistent is not None
    assert inconsistent.confidence < consistent.confidence


def test_migration_adds_collect_status_column_with_ok_default(tmp_path):
    """ALTER TABLE で collect_status が追加され、既存行は 'ok' で埋まる。"""
    from sqlalchemy import create_engine, text

    db_path = tmp_path / "test.db"
    # 旧スキーマで 1 行 INSERT (collect_status カラム無し状態をシミュレート)。
    # _get_engine は create_all で全 ORM カラムを作ってしまうので、ここでは raw engine を使う。
    raw_engine = create_engine(f"sqlite:///{db_path}", echo=False)
    with raw_engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE technical_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "symbol VARCHAR NOT NULL, "
            "analyzed_at DATETIME NOT NULL, "
            "bias_score FLOAT, confidence FLOAT, direction_bias VARCHAR, "
            "stop_loss FLOAT, take_profit FLOAT, "
            "entry_zone_low FLOAT, entry_zone_high FLOAT, "
            "risk_reward_ratio FLOAT, reasoning_summary VARCHAR, "
            "market_regime VARCHAR, confidence_modifier FLOAT)"
        ))
        conn.execute(text(
            "INSERT INTO technical_snapshots (symbol, analyzed_at) "
            "VALUES ('USDJPY=X', '2026-05-01 12:00:00')"
        ))
        conn.commit()
    raw_engine.dispose()

    # 新 AnalysisStore を生成 → migration 走行
    AnalysisStore(db_path)

    # 既存行に collect_status='ok' が埋まる
    verify_engine = create_engine(f"sqlite:///{db_path}", echo=False)
    with verify_engine.connect() as conn:
        result = conn.execute(text(
            "SELECT collect_status FROM technical_snapshots WHERE symbol='USDJPY=X'"
        )).scalar_one()
    verify_engine.dispose()
    assert result == "ok"


def test_add_snapshot_writes_ok_status_row(store: AnalysisStore):
    """add_snapshot は collect_status='ok' で INSERT する。"""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from src.data.analysis_store import _TechnicalSnapshot

    store.add_snapshot(_snapshot(bias=0.2))

    with Session(store._engine) as session:
        rows = list(session.execute(
            select(_TechnicalSnapshot)
            .where(_TechnicalSnapshot.symbol == "USDJPY=X")
        ).scalars())
    assert len(rows) == 1
    assert rows[0].collect_status == "ok"
    assert rows[0].bias_score == 0.2


def test_add_sentinel_writes_stale_price_row(store: AnalysisStore):
    """stale_price sentinel は collect_status='stale_price'、bias=conf=0、reason 保存。"""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from src.data.analysis_store import _TechnicalSnapshot

    store.add_sentinel(
        symbol="USDJPY=X",
        status="stale_price",
        reason="latest bar 7:00:00 ago",
    )
    with Session(store._engine) as session:
        rows = list(session.execute(
            select(_TechnicalSnapshot)
            .where(_TechnicalSnapshot.symbol == "USDJPY=X")
        ).scalars())
    assert len(rows) == 1
    r = rows[0]
    assert r.collect_status == "stale_price"
    assert r.direction_bias == "neutral"
    assert r.bias_score == 0.0
    assert r.confidence == 0.0
    assert r.stop_loss == 0.0
    assert r.take_profit == 0.0
    assert r.entry_zone_low == 0.0
    assert r.entry_zone_high == 0.0
    assert r.risk_reward_ratio == 0.0
    assert r.market_regime == "unknown"
    assert r.confidence_modifier == 0.0
    assert r.reasoning_summary == "latest bar 7:00:00 ago"


def test_add_sentinel_writes_failed_row(store: AnalysisStore):
    """failed sentinel も同様に書ける。"""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from src.data.analysis_store import _TechnicalSnapshot

    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="llm_error: TimeoutError")
    with Session(store._engine) as session:
        r = session.execute(
            select(_TechnicalSnapshot).where(_TechnicalSnapshot.symbol == "USDJPY=X")
        ).scalar_one()
    assert r.collect_status == "failed"
    assert r.reasoning_summary == "llm_error: TimeoutError"


def test_add_sentinel_invalid_status_raises(store: AnalysisStore):
    """status バリデーション: 許可外で ValueError。"""
    with pytest.raises(ValueError, match="sentinel status"):
        store.add_sentinel(symbol="USDJPY=X", status="weird", reason="x")


def test_add_sentinel_long_reason_truncated(store: AnalysisStore):
    """reason が 512 文字超なら truncate されて '... [truncated]' が付く。"""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from src.data.analysis_store import _TechnicalSnapshot

    long_reason = "x" * 1000
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason=long_reason)
    with Session(store._engine) as session:
        r = session.execute(
            select(_TechnicalSnapshot).where(_TechnicalSnapshot.symbol == "USDJPY=X")
        ).scalar_one()
    assert len(r.reasoning_summary) <= 512 + len(" ... [truncated]")
    assert r.reasoning_summary.endswith(" ... [truncated]")
    assert r.reasoning_summary.startswith("x" * 512)


def test_get_recent_ok_snapshots_excludes_sentinel(store: AnalysisStore):
    """ok + sentinel 混在 → ok のみ返す (sentinel は除外)。"""
    store.add_snapshot(_snapshot(bias=0.3, hours_ago=1))
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="x")
    store.add_snapshot(_snapshot(bias=0.4, hours_ago=0.5))
    rows = store.get_recent_ok_snapshots("USDJPY=X", hours=8)
    assert len(rows) == 2
    assert all(r.collect_status == "ok" for r in rows)


def test_aggregate_ignores_sentinel(store: AnalysisStore):
    """sentinel 混在でも aggregate は ok のみで集計する。"""
    store.add_snapshot(_snapshot(direction="long", bias=0.5, hours_ago=0))
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="x")
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "long"
    assert result.bias_score > 0.4  # sentinel の bias=0 が混ざっていれば下がる


def test_aggregate_with_only_sentinel_returns_none(store: AnalysisStore):
    """sentinel のみ (ok 行ゼロ) → aggregate は None。"""
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="x")
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="y")
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is None
