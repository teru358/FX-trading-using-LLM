"""compute_multi_tf_technical_score のテスト。"""
from __future__ import annotations

import pytest

from src.signals.technical_scorer import (
    MultiTfTechnicalScore,
    TechnicalScore,
    compute_multi_tf_technical_score,
)


def _ts(total: float, direction: str = "long", confidence: float = 0.7) -> TechnicalScore:
    """テスト用ミニマル TechnicalScore ヘルパ。"""
    return TechnicalScore(
        sma_score=total, rsi_score=0.0, macd_score=0.0,
        ichimoku_score=0.0, bb_score=0.0, pattern_score=0.0,
        adx_factor=1.0,
        total_score=total,
        confidence=confidence,
        direction=direction,
    )


_WEIGHTS = {"long": 0.40, "medium": 0.35, "short": 0.25}


# ── 全 TF 一致 ───────────────────────────────────────────────


def test_all_tfs_bullish_aligned():
    """全 TF が long 方向 → alignment=1.0、final_score は raw_score 相当。"""
    scores = {
        "long":   _ts(+0.6, "long", 0.8),
        "medium": _ts(+0.5, "long", 0.7),
        "short":  _ts(+0.7, "long", 0.75),
    }
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)

    # raw_score = 0.6*0.4 + 0.5*0.35 + 0.7*0.25 = 0.24 + 0.175 + 0.175 = 0.590
    assert result.raw_score == pytest.approx(0.590)
    assert result.alignment == 1.0
    # alignment 1.0 → final = raw × (0.5 + 0.5*1.0) = raw × 1.0
    assert result.total_score == pytest.approx(0.590)
    assert result.direction == "long"
    # confidence は weighted avg × (0.8 + 0.2*1.0) = 1.0 倍
    # weighted_conf = 0.8*0.4 + 0.7*0.35 + 0.75*0.25 = 0.7525
    assert result.confidence == pytest.approx(0.7525, rel=0.01)


def test_all_tfs_bearish_aligned():
    scores = {
        "long":   _ts(-0.5, "short", 0.8),
        "medium": _ts(-0.4, "short", 0.7),
        "short":  _ts(-0.6, "short", 0.7),
    }
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)

    assert result.alignment == 1.0
    assert result.direction == "short"
    assert result.total_score < -0.4


# ── TF 間不一致 ──────────────────────────────────────────────


def test_mixed_directions_dampens_score():
    """長期 bullish + 短期 bearish → alignment 低下で final_score 減衰。"""
    scores = {
        "long":   _ts(+0.6, "long", 0.8),
        "medium": _ts(+0.3, "long", 0.6),
        "short":  _ts(-0.5, "short", 0.7),
    }
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)

    # raw_score = 0.6*0.4 + 0.3*0.35 + (-0.5)*0.25 = 0.24 + 0.105 - 0.125 = 0.22
    assert result.raw_score == pytest.approx(0.22)
    # 2 long + 1 short → alignment 0.67
    assert result.alignment == 0.67
    # final = 0.22 × (0.5 + 0.5 × 0.67) = 0.22 × 0.835 = 0.1837
    assert result.total_score == pytest.approx(0.22 * 0.835, rel=0.01)


def test_full_conflict_heavy_dampening():
    """1 long / 1 short / 1 neutral → 混在 alignment 0.33 で大きく減衰。"""
    scores = {
        "long":   _ts(+0.6, "long", 0.7),
        "medium": _ts(0.0, "neutral", 0.5),
        "short":  _ts(-0.6, "short", 0.7),
    }
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)

    # raw_score = 0.6*0.4 + 0*0.35 + (-0.6)*0.25 = 0.24 - 0.15 = 0.09
    assert result.raw_score == pytest.approx(0.09)
    # 1 long + 1 short → alignment 0.33 (ミックス)
    assert result.alignment == 0.33
    # final = 0.09 × (0.5 + 0.5 × 0.33) = 0.09 × 0.665 = 0.06
    # → direction は 0.05 超で "long" だが confidence は低いはず
    assert abs(result.total_score) < 0.1


# ── 欠落 TF (リサンプル失敗等) ───────────────────────────────


def test_missing_tf_renormalizes_weights():
    """long TF が欠落 → medium + short のみで重み再正規化。"""
    scores = {
        "medium": _ts(+0.5, "long", 0.7),
        "short":  _ts(+0.4, "long", 0.7),
    }
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)

    # active_weights = {medium: 0.35, short: 0.25}, total=0.6
    # raw_score = (0.5 × 0.35 + 0.4 × 0.25) / 0.6 = (0.175 + 0.1)/0.6 = 0.4583
    assert result.raw_score == pytest.approx((0.175 + 0.1) / 0.6, rel=0.01)
    # 2 TF とも long → alignment 1.0
    assert result.alignment == 1.0
    assert result.direction == "long"
    # long TF は tf_weights に含まれない
    assert "long" not in result.tf_weights
    assert "medium" in result.tf_weights
    assert "short" in result.tf_weights


def test_only_short_tf_works():
    """short のみ残存 → それが 100% weight として使われる。"""
    scores = {"short": _ts(+0.6, "long", 0.7)}
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)

    # alignment: non_zero が 1 つ → 0.67
    assert result.alignment == 0.67
    # raw_score = 0.6 (100% weight)
    assert result.raw_score == pytest.approx(0.6)
    # final = 0.6 × (0.5 + 0.5*0.67) = 0.6 × 0.835 = 0.501
    assert result.total_score == pytest.approx(0.6 * 0.835, rel=0.01)


def test_all_missing_returns_neutral():
    result = compute_multi_tf_technical_score({}, _WEIGHTS)
    assert result.direction == "neutral"
    assert result.total_score == 0.0
    assert result.tf_scores == {}


# ── as_technical_score 互換変換 ─────────────────────────────


def test_as_technical_score_uses_short_tf_categories():
    """as_technical_score() は短期 TF のカテゴリ別スコアを返す。"""
    short = TechnicalScore(
        sma_score=0.5, rsi_score=0.3, macd_score=0.4, ichimoku_score=0.6,
        bb_score=0.2, pattern_score=0.1,
        adx_factor=1.0, total_score=0.5, confidence=0.7, direction="long",
    )
    scores = {
        "long":   _ts(+0.7, "long", 0.8),
        "medium": _ts(+0.5, "long", 0.7),
        "short":  short,
    }
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)
    compat = result.as_technical_score()

    # カテゴリ別は short TF 由来
    assert compat.sma_score == 0.5
    assert compat.rsi_score == 0.3
    assert compat.ichimoku_score == 0.6
    # total/direction/confidence は MTF 合成結果
    assert compat.total_score == result.total_score
    assert compat.direction == result.direction
    assert compat.confidence == result.confidence


# ── format_for_prompt ────────────────────────────────────────


def test_format_for_prompt_includes_all_tfs():
    scores = {
        "long":   _ts(+0.6, "long", 0.8),
        "medium": _ts(+0.5, "long", 0.7),
        "short":  _ts(+0.7, "long", 0.75),
    }
    result = compute_multi_tf_technical_score(scores, _WEIGHTS)
    text = result.format_for_prompt()

    assert "long" in text
    assert "medium" in text
    assert "short" in text
    assert "alignment" in text
