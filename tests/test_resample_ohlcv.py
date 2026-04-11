"""resample_ohlcv のテスト。"""
from __future__ import annotations

import pandas as pd
import pytest

from src.data.resample import resample_ohlcv


def _make_1h_df(n: int = 24) -> pd.DataFrame:
    """n 本の連続 1h OHLCV を作る。各バーの OHLC は識別しやすい一意値。"""
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "Open":   [100.0 + i * 0.1 for i in range(n)],
            "High":   [100.0 + i * 0.1 + 0.5 for i in range(n)],
            "Low":    [100.0 + i * 0.1 - 0.5 for i in range(n)],
            "Close":  [100.0 + i * 0.1 + 0.2 for i in range(n)],
            "Volume": [1000 + i for i in range(n)],
        },
        index=idx,
    )


# ── identity ─────────────────────────────────────────────


def test_resample_1h_returns_copy_equal():
    df = _make_1h_df(10)
    out = resample_ohlcv(df, "1h")
    pd.testing.assert_frame_equal(out, df)
    assert out is not df, "should return a copy, not the same object"


# ── 4h aggregation ───────────────────────────────────────


def test_resample_4h_aggregates_correctly():
    """4h リサンプル: 4 本の 1h から 1 本の 4h を生成。

    Open: 最初のバー
    High: 4 本中の最大
    Low:  4 本中の最小
    Close: 最後のバー
    Volume: 4 本の合計
    """
    df = _make_1h_df(8)   # 8 時間 → 4h バケット 2 個
    out = resample_ohlcv(df, "4h")

    assert len(out) == 2
    # 最初の 4h バケット (0-3 時)
    b0 = out.iloc[0]
    assert b0["Open"] == df.iloc[0]["Open"]      # 最初の Open
    assert b0["High"] == max(df.iloc[0:4]["High"])  # 4 本中 max
    assert b0["Low"] == min(df.iloc[0:4]["Low"])    # 4 本中 min
    assert b0["Close"] == df.iloc[3]["Close"]    # 最後の Close
    assert b0["Volume"] == sum(df.iloc[0:4]["Volume"])


def test_resample_4h_from_many_bars():
    """90 日 × 1h = 2160 本を 4h にすると 540 本前後になる。"""
    df = _make_1h_df(24 * 14)  # 14 日分
    out = resample_ohlcv(df, "4h")
    # 14 日 × 6 バケット/日 = 84 本前後 (境界次第で ±1)
    assert 83 <= len(out) <= 85


# ── 1d aggregation ───────────────────────────────────────


def test_resample_1d_aggregates_24h_to_1_bar():
    df = _make_1h_df(24)
    out = resample_ohlcv(df, "1d")
    assert len(out) == 1
    bar = out.iloc[0]
    assert bar["Open"] == df.iloc[0]["Open"]
    assert bar["High"] == df["High"].max()
    assert bar["Low"] == df["Low"].min()
    assert bar["Close"] == df.iloc[-1]["Close"]
    assert bar["Volume"] == df["Volume"].sum()


def test_resample_1d_from_90_days():
    """90 日 × 24 本の 1h → 90 本の 1d に集約される。"""
    df = _make_1h_df(24 * 90)
    out = resample_ohlcv(df, "1d")
    assert len(out) == 90


# ── error cases ──────────────────────────────────────────


def test_resample_unknown_interval_raises():
    df = _make_1h_df(10)
    with pytest.raises(ValueError, match="Unsupported interval"):
        resample_ohlcv(df, "3h")


# ── edge case: 不完全バケット ─────────────────────────────


def test_resample_drops_empty_buckets():
    """NaN が出るバケットは dropna で除去される。"""
    df = _make_1h_df(6)  # 6 時間 → 4h バケット 2 個 (2 本目は 2 bar のみ)
    out = resample_ohlcv(df, "4h")
    # すべてのバーが NaN 無しで残る
    assert not out.isna().any().any()
    assert len(out) == 2
