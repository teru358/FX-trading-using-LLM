"""ルールベーステクニカルスコアリングエンジンのテスト。"""
from __future__ import annotations

import pytest

from src.config import IndicatorToggleConfig
from src.data.indicators import IndicatorSummary
from src.signals.technical_scorer import TechnicalScore, compute_technical_score


def _base_summary(**overrides) -> IndicatorSummary:
    """sensible defaults で IndicatorSummary を生成するヘルパー。"""
    defaults = dict(
        current_price=150.0,
        sma_20=149.0,
        sma_50=148.0,
        sma_200=145.0,
        ema_12=149.5,
        ema_26=148.5,
        rsi_14=50.0,
        macd_line=0.05,
        macd_signal=0.03,
        macd_histogram=0.02,
        bb_upper=152.0,
        bb_lower=146.0,
        bb_pct_b=0.5,
        atr_14=1.0,
        adx_14=25.0,
        ichimoku_signal="neutral",
        pattern_bias="neutral",
    )
    defaults.update(overrides)
    return IndicatorSummary(**defaults)


# ---------------------------------------------------------------------------
# SMA Alignment tests
# ---------------------------------------------------------------------------

def test_sma_full_bullish_alignment():
    """price>SMA20>SMA50>SMA200 → sma_score が 0.9 超。"""
    s = _base_summary(
        current_price=155.0,
        sma_20=153.0,
        sma_50=150.0,
        sma_200=145.0,
    )
    score = compute_technical_score(s)
    assert score.sma_score > 0.9


def test_sma_full_bearish_alignment():
    """price<SMA20<SMA50<SMA200 → sma_score が -0.9 未満。"""
    s = _base_summary(
        current_price=140.0,
        sma_20=143.0,
        sma_50=147.0,
        sma_200=152.0,
    )
    score = compute_technical_score(s)
    assert score.sma_score < -0.9


def test_sma_mixed():
    """一部だけ上 → sma_score が -0.5〜0.5 の範囲内。"""
    s = _base_summary(
        current_price=150.0,
        sma_20=151.0,   # price < SMA20 (bearish)
        sma_50=149.0,   # SMA20 > SMA50 (bullish)
        sma_200=148.0,  # SMA50 > SMA200 (bullish)
    )
    score = compute_technical_score(s)
    assert -0.5 < score.sma_score < 0.5


# ---------------------------------------------------------------------------
# RSI tests
# ---------------------------------------------------------------------------

def test_rsi_overbought():
    """RSI=75 → rsi_score が 0.5 超。"""
    s = _base_summary(rsi_14=75.0)
    score = compute_technical_score(s)
    assert score.rsi_score > 0.5


def test_rsi_oversold():
    """RSI=25 → rsi_score が -0.5 未満。"""
    s = _base_summary(rsi_14=25.0)
    score = compute_technical_score(s)
    assert score.rsi_score < -0.5


def test_rsi_neutral():
    """RSI=50 → rsi_score がほぼ 0。"""
    s = _base_summary(rsi_14=50.0)
    score = compute_technical_score(s)
    assert abs(score.rsi_score) < 0.1


# ---------------------------------------------------------------------------
# MACD tests
# ---------------------------------------------------------------------------

def test_macd_bullish():
    """histogram>0 かつ line>signal → macd_score > 0.3。"""
    s = _base_summary(
        macd_histogram=0.1,
        macd_line=0.2,
        macd_signal=0.1,
    )
    score = compute_technical_score(s)
    assert score.macd_score > 0.3


def test_macd_bearish():
    """histogram<0 かつ line<signal → macd_score < -0.3。"""
    s = _base_summary(
        macd_histogram=-0.1,
        macd_line=-0.2,
        macd_signal=-0.1,
    )
    score = compute_technical_score(s)
    assert score.macd_score < -0.3


# ---------------------------------------------------------------------------
# Ichimoku tests
# ---------------------------------------------------------------------------

def test_ichimoku_strong_bullish():
    """strong_bullish → ichimoku_score > 0.9。"""
    s = _base_summary(ichimoku_signal="strong_bullish")
    score = compute_technical_score(s)
    assert score.ichimoku_score > 0.9


def test_ichimoku_strong_bearish():
    """strong_bearish → ichimoku_score < -0.9。"""
    s = _base_summary(ichimoku_signal="strong_bearish")
    score = compute_technical_score(s)
    assert score.ichimoku_score < -0.9


# ---------------------------------------------------------------------------
# Bollinger Band tests
# ---------------------------------------------------------------------------

def test_bb_upper_band():
    """%B=0.9 → bb_score > 0.3。"""
    s = _base_summary(bb_pct_b=0.9)
    score = compute_technical_score(s)
    assert score.bb_score > 0.3


# ---------------------------------------------------------------------------
# Pattern tests
# ---------------------------------------------------------------------------

def test_pattern_bullish():
    """pattern_bias=bullish → pattern_score > 0.5。"""
    s = _base_summary(pattern_bias="bullish")
    score = compute_technical_score(s)
    assert score.pattern_score > 0.5


def test_pattern_bearish():
    """pattern_bias=bearish → pattern_score < -0.5。"""
    s = _base_summary(pattern_bias="bearish")
    score = compute_technical_score(s)
    assert score.pattern_score < -0.5


# ---------------------------------------------------------------------------
# Combined / ADX tests
# ---------------------------------------------------------------------------

def test_combined_score_range():
    """total_score は常に [-1, 1] の範囲内。"""
    s = _base_summary(
        current_price=155.0,
        sma_20=153.0,
        sma_50=150.0,
        sma_200=145.0,
        rsi_14=80.0,
        macd_histogram=0.5,
        macd_line=0.5,
        macd_signal=0.1,
        ichimoku_signal="strong_bullish",
        bb_pct_b=0.95,
        pattern_bias="bullish",
        adx_14=40.0,
    )
    score = compute_technical_score(s)
    assert -1.0 <= score.total_score <= 1.0


def test_adx_low_dampens_score():
    """ADX=10 のスコアは ADX=40 のスコアより有意に小さい。"""
    base_kwargs = dict(
        current_price=155.0,
        sma_20=153.0,
        sma_50=150.0,
        sma_200=145.0,
        rsi_14=75.0,
        macd_histogram=0.2,
        macd_line=0.2,
        macd_signal=0.1,
        ichimoku_signal="bullish",
        bb_pct_b=0.85,
        pattern_bias="bullish",
    )
    weak = compute_technical_score(_base_summary(adx_14=10.0, **base_kwargs))
    strong = compute_technical_score(_base_summary(adx_14=40.0, **base_kwargs))
    # weak は strong の 1.5 倍未満（dampened）
    assert abs(weak.total_score) < abs(strong.total_score) * 1.5
    assert abs(weak.total_score) < abs(strong.total_score)


# ---------------------------------------------------------------------------
# Confidence tests
# ---------------------------------------------------------------------------

def test_confidence_high_when_aligned():
    """全シグナルが強気 → confidence > 0.7。"""
    s = _base_summary(
        current_price=155.0,
        sma_20=153.0,
        sma_50=150.0,
        sma_200=145.0,
        rsi_14=70.0,
        macd_histogram=0.2,
        macd_line=0.2,
        macd_signal=0.1,
        ichimoku_signal="strong_bullish",
        bb_pct_b=0.85,
        pattern_bias="bullish",
        adx_14=35.0,
    )
    score = compute_technical_score(s)
    assert score.confidence > 0.7


def test_confidence_low_when_conflicting():
    """強気と弱気のシグナルが混在 → confidence < 0.5。"""
    s = _base_summary(
        current_price=155.0,
        sma_20=153.0,
        sma_50=150.0,
        sma_200=145.0,
        rsi_14=25.0,          # 弱気
        macd_histogram=-0.2,  # 弱気
        macd_line=-0.1,
        macd_signal=0.1,
        ichimoku_signal="strong_bearish",  # 弱気
        bb_pct_b=0.85,        # 強気
        pattern_bias="bullish",  # 強気
        adx_14=12.0,
    )
    score = compute_technical_score(s)
    assert score.confidence < 0.5


# ---------------------------------------------------------------------------
# Direction tests
# ---------------------------------------------------------------------------

def test_direction_from_score_long():
    """強気セットアップ → direction が 'long'。"""
    s = _base_summary(
        current_price=155.0,
        sma_20=153.0,
        sma_50=150.0,
        sma_200=145.0,
        rsi_14=70.0,
        macd_histogram=0.2,
        macd_line=0.2,
        macd_signal=0.1,
        ichimoku_signal="strong_bullish",
        pattern_bias="bullish",
        adx_14=30.0,
    )
    score = compute_technical_score(s)
    assert score.direction == "long"


def test_direction_from_score_short():
    """弱気セットアップ → direction が 'short'。"""
    s = _base_summary(
        current_price=140.0,
        sma_20=143.0,
        sma_50=147.0,
        sma_200=152.0,
        rsi_14=25.0,
        macd_histogram=-0.2,
        macd_line=-0.2,
        macd_signal=-0.1,
        ichimoku_signal="strong_bearish",
        pattern_bias="bearish",
        adx_14=30.0,
    )
    score = compute_technical_score(s)
    assert score.direction == "short"


# ---------------------------------------------------------------------------
# format_for_prompt test
# ---------------------------------------------------------------------------

def test_format_for_prompt():
    """format_for_prompt() の出力に SMA, RSI, total/bias の文字列が含まれる。"""
    s = _base_summary()
    score = compute_technical_score(s)
    text = score.format_for_prompt()
    assert "SMA" in text
    assert "RSI" in text


# ---------------------------------------------------------------------------
# Disabled indicator tests (regression guard for weight re-normalization bug)
#
# Background: compute_indicators() returns 0.0 defaults for disabled indicators
# via safe() fallback. Prior to the fix, compute_technical_score treated
# rsi=0 as "below 40 → bearish -1.0", sma_20=sma_50=sma_200=0 as "price > 0 fail → -1.0",
# etc., which artificially pushed total_score bearish whenever any indicator
# was disabled in the config. The fix is to accept `indicator_cfg` and skip
# disabled categories while re-normalizing the remaining weights.
# ---------------------------------------------------------------------------


def _neutral_strong_bullish_summary() -> IndicatorSummary:
    """すべての指標が弱bullish〜中立で、ichimoku だけ strong_bullish のサマリー。

    ichimoku 単独の貢献が正に効くことを確認するための基礎データ。
    """
    return _base_summary(
        current_price=150.0,
        sma_20=150.0,
        sma_50=150.0,
        sma_200=150.0,
        rsi_14=50.0,
        macd_line=0.0,
        macd_signal=0.0,
        macd_histogram=0.0,
        bb_pct_b=0.5,
        adx_14=30.0,
        ichimoku_signal="strong_bullish",
    )


def test_disabled_rsi_does_not_push_bearish():
    """RSI 無効時に rsi=0 を受け取っても tech_score は bearish 側に倒れない。"""
    s = _base_summary(rsi_14=0.0)   # disabled 指標の既定値シミュレート
    cfg = IndicatorToggleConfig(rsi=False)

    score = compute_technical_score(s, indicator_cfg=cfg)

    # RSI カテゴリは 0 を維持 (スキップされた扱い)
    assert score.rsi_score == 0.0
    # 他が中立なので total は 0 付近。-0.1 未満の bearish にはならないこと
    assert score.total_score > -0.1


def test_disabled_bb_does_not_push_bearish():
    """BB 無効時に bb_pct_b=0 を受け取っても bearish 側に倒れない。"""
    s = _base_summary(bb_pct_b=0.0)
    cfg = IndicatorToggleConfig(bollinger_bands=False)

    score = compute_technical_score(s, indicator_cfg=cfg)

    assert score.bb_score == 0.0
    assert score.total_score > -0.1


def test_disabled_sma_does_not_push_bearish():
    """SMA 無効時に sma_*=0 を受け取っても bearish 側に倒れない。"""
    s = _base_summary(sma_20=0.0, sma_50=0.0, sma_200=0.0)
    cfg = IndicatorToggleConfig(moving_averages=False)

    score = compute_technical_score(s, indicator_cfg=cfg)

    assert score.sma_score == 0.0
    assert score.total_score > -0.1


def test_disabled_macd_does_not_push_bearish():
    """MACD 無効時に macd_*=0 を受け取っても bearish 側に倒れない。"""
    s = _base_summary(macd_line=0.0, macd_signal=0.0, macd_histogram=0.0)
    cfg = IndicatorToggleConfig(macd=False)

    score = compute_technical_score(s, indicator_cfg=cfg)

    assert score.macd_score == 0.0
    assert score.total_score > -0.1


def test_only_ichimoku_enabled_reflects_ichimoku_fully():
    """ichimoku 以外すべて disabled → total_score は ichimoku の値そのものを反映。

    weight 再正規化が機能していれば、ichimoku=strong_bullish (1.0) が total の
    主要因となり total_score > 0.5 になる。
    """
    s = _neutral_strong_bullish_summary()
    cfg = IndicatorToggleConfig(
        moving_averages=False,
        rsi=False,
        macd=False,
        bollinger_bands=False,
        atr=True,   # ATR は scoring には使われないので無関係
        adx=True,   # ADX は factor に使われる
        ichimoku=True,
    )

    score = compute_technical_score(s, indicator_cfg=cfg)

    # 無効化された指標のカテゴリスコアは 0
    assert score.sma_score == 0.0
    assert score.rsi_score == 0.0
    assert score.macd_score == 0.0
    assert score.bb_score == 0.0
    # ichimoku 単体の貢献だから total_score は大きく正 (weight 再正規化)
    assert score.ichimoku_score == 1.0
    assert score.total_score > 0.5
    assert score.direction == "long"


def test_all_disabled_returns_neutral():
    """全指標 disabled → neutral 判定で total_score ≈ 0。"""
    s = _base_summary()
    cfg = IndicatorToggleConfig(
        moving_averages=False,
        rsi=False,
        macd=False,
        bollinger_bands=False,
        ichimoku=False,
    )

    score = compute_technical_score(s, indicator_cfg=cfg)

    assert score.total_score == pytest.approx(0.0)
    assert score.direction == "neutral"


def test_default_cfg_matches_no_cfg():
    """cfg=None と 全 True cfg は完全に同じ結果を返す。"""
    s = _base_summary(
        current_price=155.0, sma_20=153.0, sma_50=150.0, sma_200=145.0,
        rsi_14=65.0, macd_line=0.3, macd_signal=0.2, macd_histogram=0.1,
        bb_pct_b=0.75, adx_14=30.0, ichimoku_signal="bullish",
    )
    a = compute_technical_score(s)
    b = compute_technical_score(s, indicator_cfg=IndicatorToggleConfig())
    assert a.total_score == pytest.approx(b.total_score)
    assert a.direction == b.direction
    assert a.confidence == pytest.approx(b.confidence)
