"""price_analyzer._validate_sl_tp と _downgrade_to_neutral のテスト。"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.analysis.price_analyzer import (
    PriceAnalysis,
    _downgrade_to_neutral,
    _validate_sl_tp,
)


def _mk(direction: str, entry: tuple[float, float], sl: float, tp: float) -> PriceAnalysis:
    return PriceAnalysis(
        pair="USDJPY=X",
        direction_bias=direction,
        bias_score=0.3,
        confidence=0.7,
        entry_zone=entry,
        stop_loss=sl,
        take_profit=tp,
        risk_reward_ratio=2.0,
        reasoning_summary="test",
        analyzed_at=datetime.now(),
    )


# ── LONG ─────────────────────────────────────────────


def test_long_valid_passes():
    a = _mk("long", (159.0, 159.5), sl=158.5, tp=160.5)
    assert _validate_sl_tp(a) is None
    assert a.stop_loss == 158.5
    assert a.take_profit == 160.5


def test_long_tp_slightly_below_entry_high_auto_corrected():
    """LONG: TP が entry_high を 1pip 程度下回る → 自動補正される (今回の実バグケース)。"""
    a = _mk("long", (159.35, 159.36782), sl=158.5, tp=159.35798)
    assert _validate_sl_tp(a) is None
    # TP は entry_high 超過に補正されている
    assert a.take_profit > 159.36782


def test_long_tp_far_below_entry_high_rejected():
    """LONG: TP が entry_high から大幅(2%以上)下回る → 補正せずエラー。"""
    a = _mk("long", (159.0, 159.5), sl=158.5, tp=155.0)
    err = _validate_sl_tp(a)
    assert err is not None
    assert "take_profit" in err
    # TP は補正されていない
    assert a.take_profit == 155.0


def test_long_sl_slightly_above_entry_low_auto_corrected():
    a = _mk("long", (159.0, 159.5), sl=159.001, tp=160.5)
    assert _validate_sl_tp(a) is None
    assert a.stop_loss < 159.0


def test_long_sl_far_above_entry_low_rejected():
    a = _mk("long", (159.0, 159.5), sl=161.0, tp=162.0)
    err = _validate_sl_tp(a)
    assert err is not None
    assert "stop_loss" in err


# ── SHORT ────────────────────────────────────────────


def test_short_valid_passes():
    a = _mk("short", (159.0, 159.5), sl=160.0, tp=158.0)
    assert _validate_sl_tp(a) is None


def test_short_tp_slightly_above_entry_low_auto_corrected():
    a = _mk("short", (159.0, 159.5), sl=160.0, tp=159.001)
    assert _validate_sl_tp(a) is None
    assert a.take_profit < 159.0


def test_short_sl_slightly_below_entry_high_auto_corrected():
    a = _mk("short", (159.0, 159.5), sl=159.499, tp=158.0)
    assert _validate_sl_tp(a) is None
    assert a.stop_loss > 159.5


def test_short_tp_far_above_entry_low_rejected():
    a = _mk("short", (159.0, 159.5), sl=160.0, tp=161.0)
    err = _validate_sl_tp(a)
    assert err is not None
    assert "take_profit" in err


# ── neutral / downgrade ─────────────────────────────


def test_neutral_always_valid():
    a = _mk("neutral", (159.0, 159.0), sl=0.0, tp=0.0)
    assert _validate_sl_tp(a) is None


def test_downgrade_to_neutral_clears_sl_tp():
    a = _mk("long", (159.0, 159.5), sl=161.0, tp=162.0)
    result = _downgrade_to_neutral(a, "test reason")
    assert result.direction_bias == "neutral"
    assert result.stop_loss == 0.0
    assert result.take_profit == 0.0
    assert result.risk_reward_ratio == 0.0
    assert result.entry_zone == (159.25, 159.25)
    assert "neutral降格: test reason" in result.reasoning_summary


def test_downgrade_preserves_bias_score():
    """降格してもテクニカル情報(bias_score, confidence, support/resistance)は維持。"""
    a = _mk("long", (159.0, 159.5), sl=161.0, tp=162.0)
    a.bias_score = 0.42
    a.confidence = 0.80
    a.key_support = 158.0
    a.key_resistance = 160.5
    result = _downgrade_to_neutral(a, "test")
    assert result.bias_score == 0.42
    assert result.confidence == 0.80
    assert result.key_support == 158.0
    assert result.key_resistance == 160.5


def test_downgrade_suffix_not_duplicated():
    """同じ理由で2回降格されてもsuffixは1つだけ。"""
    a = _mk("long", (159.0, 159.5), sl=161.0, tp=162.0)
    _downgrade_to_neutral(a, "reason1")
    _downgrade_to_neutral(a, "reason1")
    assert a.reasoning_summary.count("neutral降格: reason1") == 1
