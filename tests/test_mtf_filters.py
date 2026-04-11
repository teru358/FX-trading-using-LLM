"""MTF 設定フィルタのテスト。"""
from __future__ import annotations

import pytest

from src.config import ChartPatternConfig, IndicatorToggleConfig
from src.data.mtf import filter_indicator_cfg_for_tf, filter_pattern_cfg_for_tf


# ── indicator: regime ────────────────────────────────────────


def test_regime_disables_rsi_macd_bb_atr():
    base = IndicatorToggleConfig()   # 全 True (デフォルト)
    out = filter_indicator_cfg_for_tf(base, "regime")

    assert out.moving_averages is True  # 保持
    assert out.ichimoku is True          # 保持
    assert out.adx is True               # 保持
    assert out.rsi is False              # disabled
    assert out.macd is False             # disabled
    assert out.bollinger_bands is False  # disabled
    assert out.atr is False              # disabled


def test_regime_respects_user_disabled():
    """user が ichimoku: false なら regime でも ichimoku は false。"""
    base = IndicatorToggleConfig(ichimoku=False)
    out = filter_indicator_cfg_for_tf(base, "regime")

    assert out.ichimoku is False   # user 設定を踏襲
    assert out.moving_averages is True


# ── indicator: structure ─────────────────────────────────────


def test_structure_disables_ichimoku_and_momentum():
    base = IndicatorToggleConfig()
    out = filter_indicator_cfg_for_tf(base, "structure")

    assert out.moving_averages is True
    assert out.adx is True
    assert out.ichimoku is False         # structure は ichimoku も落とす
    assert out.rsi is False
    assert out.macd is False
    assert out.bollinger_bands is False
    assert out.atr is False


# ── indicator: full ──────────────────────────────────────────


def test_full_is_identity():
    """full は追加 disable なし → base と同じ。"""
    base = IndicatorToggleConfig(rsi=False)  # user が rsi: false
    out = filter_indicator_cfg_for_tf(base, "full")

    # すべてのフィールドが base と一致
    assert out.rsi is False
    assert out.moving_averages is True
    assert out.ichimoku is True


# ── patterns: regime ─────────────────────────────────────────


def test_regime_patterns_disable_noisy_candles_and_shortterm_shapes():
    base = ChartPatternConfig(
        # 全パターン True にして「regime が何を落とすか」を明確に
        hammer=True, shooting_star=True, engulfing=True, doji=True,
        morning_evening_star=True, three_soldiers_crows=True,
        pin_bar=True, inside_bar=True,
        double_top_bottom=True, head_shoulders=True,
        triangle=True, range_bound=True,
        bb_squeeze=True, atr_contraction=True, sr_breakout=True,
    )
    out = filter_pattern_cfg_for_tf(base, "regime")

    # 長期で有効なパターン (保持)
    assert out.hammer is True
    assert out.shooting_star is True
    assert out.engulfing is True
    assert out.morning_evening_star is True
    assert out.three_soldiers_crows is True
    assert out.pin_bar is True
    assert out.double_top_bottom is True
    assert out.head_shoulders is True
    assert out.triangle is True
    assert out.range_bound is True

    # 長期で disable すべきパターン
    assert out.doji is False
    assert out.inside_bar is False
    assert out.bb_squeeze is False
    assert out.atr_contraction is False
    assert out.sr_breakout is False


# ── patterns: structure ──────────────────────────────────────


def test_structure_is_pattern_identity():
    """structure は全パターンを残す (4h × 14d はパターン検出の主戦場)。"""
    base = ChartPatternConfig(
        hammer=True, doji=True, double_top_bottom=True,
        bb_squeeze=True, triangle=True,
    )
    out = filter_pattern_cfg_for_tf(base, "structure")

    assert out.hammer is True
    assert out.doji is True
    assert out.double_top_bottom is True
    assert out.bb_squeeze is True
    assert out.triangle is True


# ── patterns: full ───────────────────────────────────────────


def test_full_disables_shape_patterns():
    """short TF (1h × 2d = 48 本) で shape パターンは検出困難 → 強制 off。"""
    base = ChartPatternConfig(
        hammer=True, doji=True,
        double_top_bottom=True, head_shoulders=True,
        triangle=True, range_bound=True,
        bb_squeeze=True, sr_breakout=True,
    )
    out = filter_pattern_cfg_for_tf(base, "full")

    # shape パターンは disabled
    assert out.double_top_bottom is False
    assert out.head_shoulders is False
    assert out.triangle is False
    assert out.range_bound is False

    # candle / squeeze / breakout 系は保持
    assert out.hammer is True
    assert out.doji is True
    assert out.bb_squeeze is True
    assert out.sr_breakout is True


# ── user false は永続 ─────────────────────────────────────────


def test_user_false_propagates_across_all_subsets():
    """user が RSI: false にしていれば、どの subset でも RSI は false。"""
    base = IndicatorToggleConfig(rsi=False)
    for subset in ("regime", "structure", "full"):
        out = filter_indicator_cfg_for_tf(base, subset)
        assert out.rsi is False, f"rsi should remain False in {subset}"
