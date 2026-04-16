"""vol_regime.compute_vol_regime() のテスト。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.signals.vol_regime import compute_vol_regime


def _make_ohlcv(n: int = 100, base: float = 150.0, volatility: float = 1.0):
    """固定ボラティリティの合成 OHLCV を生成。"""
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    np.random.seed(42)
    close = np.full(n, base) + np.random.randn(n) * 0.01
    high = close + volatility
    low = close - volatility
    return pd.DataFrame({
        "Open": close,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": np.zeros(n),
    }, index=idx)


def test_returns_none_on_insufficient_data():
    """データ不足なら None。"""
    df = _make_ohlcv(n=20)
    result = compute_vol_regime(df, ewma_span=20)
    assert result is None


def test_normal_regime_stable_vol():
    """一定ボラでは ratio ≈ 1.0、regime=normal。"""
    df = _make_ohlcv(n=100, volatility=1.0)
    result = compute_vol_regime(df, ewma_span=20)
    assert result is not None
    assert result.regime == "normal"
    assert 0.9 <= result.ratio <= 1.1
    assert result.risk_scale == 1.0


def test_high_regime_spike():
    """終盤にボラが急騰すると high 判定。"""
    df = _make_ohlcv(n=100, volatility=1.0)
    # 最後の 5 本を高ボラに差し替え
    df.iloc[-5:, df.columns.get_loc("High")] += 3.0
    df.iloc[-5:, df.columns.get_loc("Low")] -= 3.0
    result = compute_vol_regime(
        df, ewma_span=20,
        high_threshold=1.3, high_risk_scale=0.5,
    )
    assert result is not None
    assert result.regime == "high"
    assert result.ratio > 1.3
    assert result.risk_scale == 0.5


def test_low_regime_calm():
    """終盤にボラが急縮小すると low 判定。"""
    df = _make_ohlcv(n=100, volatility=2.0)
    # 最後の 10 本を低ボラに差し替え
    df.iloc[-10:, df.columns.get_loc("High")] = df.iloc[-10:]["Close"] + 0.1
    df.iloc[-10:, df.columns.get_loc("Low")] = df.iloc[-10:]["Close"] - 0.1
    result = compute_vol_regime(
        df, ewma_span=20,
        low_threshold=0.7, low_risk_scale=1.0,
    )
    assert result is not None
    assert result.regime == "low"
    assert result.ratio < 0.7
    assert result.risk_scale == 1.0


def test_none_on_empty_df():
    """空 DataFrame なら None。"""
    assert compute_vol_regime(pd.DataFrame()) is None
    assert compute_vol_regime(None) is None


def test_risk_scale_propagated():
    """risk_scale がパラメータ通りに設定される。"""
    df = _make_ohlcv(n=100, volatility=1.0)
    df.iloc[-5:, df.columns.get_loc("High")] += 3.0
    df.iloc[-5:, df.columns.get_loc("Low")] -= 3.0
    result = compute_vol_regime(
        df, ewma_span=20,
        high_threshold=1.3, high_risk_scale=0.3,
    )
    assert result is not None
    assert result.risk_scale == 0.3
