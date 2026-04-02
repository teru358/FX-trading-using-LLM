# tests/test_rag_adjustment.py
from __future__ import annotations

import pytest

from src.signals.rag_adjustment import compute_rag_adjustment, RagAdjustmentConfig


def _make_hits(outcomes: list[str], distances: list[float]) -> list[dict]:
    """テスト用のRAG検索結果を生成する。"""
    return [
        {
            "text": f"trade {i}",
            "metadata": {
                "outcome": outcome,
                "session_type": "trade",
                "phase": "complete",
            },
            "distance": dist,
        }
        for i, (outcome, dist) in enumerate(zip(outcomes, distances))
    ]


def test_bullish_with_high_win_rate():
    """bullishシグナル + bullish側の勝率が高い → 上方補正。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["win", "win", "win", "loss"], [0.2, 0.3, 0.25, 0.4])
    opposite_hits = _make_hits(["win", "loss"], [0.8, 0.9])
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj > 0


def test_bullish_with_low_win_rate():
    """bullishシグナル + bullish側の勝率が低い → 下方補正。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["loss", "loss", "loss", "win"], [0.2, 0.3, 0.25, 0.4])
    opposite_hits = _make_hits(["win"], [0.9])
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj < 0


def test_high_opposite_similarity_penalizes():
    """対向コレクションの類似度が高い → 補正が負方向に。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["win", "win"], [0.3, 0.3])
    opposite_hits = _make_hits(["win", "win", "win"], [0.1, 0.1, 0.15])
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj < cfg.same_direction_weight * (1.0 - 0.5)


def test_clamped_to_max():
    """補正値がmax_adjustmentにクランプされる。"""
    cfg = RagAdjustmentConfig(max_adjustment=0.05)
    same_hits = _make_hits(["win"] * 5, [0.1] * 5)
    opposite_hits = []
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert abs(adj) <= 0.05


def test_insufficient_hits_returns_zero():
    """ヒット数がmin_hits未満 → 補正なし。"""
    cfg = RagAdjustmentConfig(min_hits=2)
    same_hits = _make_hits(["win"], [0.3])
    opposite_hits = []
    adj = compute_rag_adjustment(
        combined_score=0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj == 0.0


def test_bearish_signal_symmetric():
    """bearishシグナルでも対称的に動作する。"""
    cfg = RagAdjustmentConfig()
    same_hits = _make_hits(["win", "win", "win"], [0.2, 0.3, 0.25])
    opposite_hits = _make_hits(["loss"], [0.8])
    adj = compute_rag_adjustment(
        combined_score=-0.30,
        same_direction_hits=same_hits,
        opposite_direction_hits=opposite_hits,
        config=cfg,
    )
    assert adj < 0


def test_weight_multipliers():
    """session_typeによる重み付けが反映される。"""
    cfg = RagAdjustmentConfig(
        trade_weight_multiplier=1.0,
        forecast_weight_multiplier=0.5,
    )
    trade_hits = [
        {"text": "t", "metadata": {"outcome": "win", "session_type": "trade", "phase": "complete"}, "distance": 0.3},
        {"text": "t", "metadata": {"outcome": "win", "session_type": "trade", "phase": "complete"}, "distance": 0.3},
    ]
    forecast_hits = [
        {"text": "f", "metadata": {"outcome": "win", "session_type": "forecast", "phase": "complete"}, "distance": 0.3},
        {"text": "f", "metadata": {"outcome": "win", "session_type": "forecast", "phase": "complete"}, "distance": 0.3},
    ]
    adj_trade = compute_rag_adjustment(0.30, trade_hits, [], cfg)
    adj_forecast = compute_rag_adjustment(0.30, forecast_hits, [], cfg)
    assert abs(adj_trade) > abs(adj_forecast)
