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
    store.upsert_snapshot(_snapshot(bias=0.06))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "long"


def test_aggregate_direction_threshold_short(store: AnalysisStore):
    """bias < -0.05 → direction = 'short'"""
    store.upsert_snapshot(_snapshot(direction="short", bias=-0.06))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "short"


def test_aggregate_direction_threshold_neutral(store: AnalysisStore):
    """-0.05 ≤ bias ≤ 0.05 → direction = 'neutral'"""
    store.upsert_snapshot(_snapshot(direction="neutral", bias=0.04))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "neutral"


def test_aggregate_direction_boundary_exactly_0_05(store: AnalysisStore):
    """bias == 0.05 は neutral (> 0.05 が long の条件)。"""
    store.upsert_snapshot(_snapshot(direction="neutral", bias=0.05))
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    assert result.direction_bias == "neutral"


# ── 時間加重 ──────────────────────────────────────────────


def test_aggregate_recent_weighted_more(store: AnalysisStore):
    """最近のスナップショットがより重く加重される。"""
    store.upsert_snapshot(_snapshot(bias=0.8, hours_ago=0))    # 最新 → weight ≈ 1.0
    store.upsert_snapshot(_snapshot(bias=-0.2, hours_ago=4))   # 4h前 → weight ≈ 0.2
    result = store.aggregate("USDJPY=X", hours=8)
    assert result is not None
    # 単純平均なら 0.3 だが、時間加重で 0.8 寄りになる
    assert result.bias_score > 0.5


# ── SL/TP 選択 ────────────────────────────────────────────


def test_aggregate_sltp_from_direction_matched_snapshot(store: AnalysisStore):
    """集約方向と一致するスナップショットの SL/TP が採用される。"""
    # 最新は short だが、集約方向が long になるケース
    store.upsert_snapshot(_snapshot(direction="long", bias=0.6, hours_ago=0,
                                   sl=148.0, tp=153.0))
    store.upsert_snapshot(_snapshot(direction="short", bias=-0.1, hours_ago=2,
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
    store_consistent.upsert_snapshot(_snapshot(direction="long", bias=0.3, confidence=0.8, hours_ago=0))
    store_consistent.upsert_snapshot(_snapshot(direction="long", bias=0.3, confidence=0.8, hours_ago=1))
    consistent = store_consistent.aggregate("USDJPY=X", hours=8)

    # 不一致ケース用に別シンボルで
    store.upsert_snapshot(_snapshot(symbol="EURUSD=X", direction="long", bias=0.3, confidence=0.8, hours_ago=0))
    store.upsert_snapshot(_snapshot(symbol="EURUSD=X", direction="short", bias=-0.3, confidence=0.8, hours_ago=1))
    inconsistent = store.aggregate("EURUSD=X", hours=8)

    assert consistent is not None
    assert inconsistent is not None
    assert inconsistent.confidence < consistent.confidence
