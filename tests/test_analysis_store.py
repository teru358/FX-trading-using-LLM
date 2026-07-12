"""AnalysisStore.aggregate() のテスト。

方向閾値・時間加重・consistency 減衰を検証する。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from src.analysis.technical_snapshot_data import TechnicalSnapshotData
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
) -> TechnicalSnapshotData:
    return TechnicalSnapshotData(
        pair=symbol,
        analyzed_at=db_now() - timedelta(hours=hours_ago),
        bias_score=bias,
        confidence=confidence,
        direction_bias=direction,
    )


# ── direction 閾値 0.05 ──────────────────────────────────


def test_aggregate_direction_threshold_long(store: AnalysisStore):
    """bias > 0.05 → direction = 'long'"""
    store.add_snapshot(_snapshot(bias=0.06))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "long"


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


# ── SL/TP 等の削除列は既定値 ──────────────────────────────


def test_aggregate_removed_columns_return_defaults(store: AnalysisStore):
    """LLM 廃止で削除された SL/TP/RR/regime は常に既定値で返る。"""
    store.add_snapshot(_snapshot(direction="long", bias=0.6, hours_ago=0))
    store.add_snapshot(_snapshot(direction="short", bias=-0.1, hours_ago=2))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.stop_loss == 0.0
    assert result.take_profit == 0.0
    assert result.entry_zone == (0.0, 0.0)
    assert result.risk_reward_ratio == 2.0
    assert result.market_regime == "unknown"
    assert result.confidence_modifier == 0.0


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
    assert r.reason == "latest bar 7:00:00 ago"


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
    assert r.reason == "llm_error: TimeoutError"


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
    assert len(r.reason) <= 512 + len(" ... [truncated]")
    assert r.reason.endswith(" ... [truncated]")
    assert r.reason.startswith("x" * 512)


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


def test_get_latest_collect_row_returns_newest_any_status(store: AnalysisStore):
    """sentinel + ok + 古い ok → 最新の sentinel が返る (status 制約なし、lookback なし)。"""
    store.add_snapshot(_snapshot(bias=0.1, hours_ago=2))
    store.add_snapshot(_snapshot(bias=0.2, hours_ago=1))
    store.add_sentinel(symbol="USDJPY=X", status="stale_price", reason="latest")
    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is not None
    assert latest.collect_status == "stale_price"


def test_get_latest_collect_row_returns_none_when_empty(store: AnalysisStore):
    """データなし → None。"""
    latest = store.get_latest_collect_row("USDJPY=X")
    assert latest is None


def test_get_latest_ok_row_skips_sentinel(store: AnalysisStore):
    """最新が sentinel + 古い ok → 古い ok が返る。"""
    store.add_snapshot(_snapshot(bias=0.3, hours_ago=2))
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="recent")
    latest_ok = store.get_latest_ok_row("USDJPY=X")
    assert latest_ok is not None
    assert latest_ok.collect_status == "ok"
    assert latest_ok.bias_score == 0.3


def test_get_latest_ok_row_returns_none_when_only_sentinel(store: AnalysisStore):
    """sentinel のみ → None。"""
    store.add_sentinel(symbol="USDJPY=X", status="failed", reason="x")
    latest_ok = store.get_latest_ok_row("USDJPY=X")
    assert latest_ok is None
