"""resample の 15m 基底足対応 (spec S-4b / codex High#1)。"""
import pandas as pd
import pytest

from src.data.resample import resample_ohlcv


def _df_15m(n=8):
    idx = pd.date_range("2026-07-01 09:00", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "Open": [float(i) for i in range(n)],
            "High": [float(i) + 1 for i in range(n)],
            "Low": [float(i) - 1 for i in range(n)],
            "Close": [float(i) + 0.5 for i in range(n)],
            "Volume": [1.0] * n,
        },
        index=idx,
    )


def test_identity_when_target_equals_base():
    df = _df_15m()
    out = resample_ohlcv(df, "15m", base_interval="15m")
    assert len(out) == len(df)


def test_15m_base_to_1h():
    df = _df_15m(8)  # 2 時間分
    out = resample_ohlcv(df, "1h", base_interval="15m")
    assert len(out) == 2
    # 1 本目 = 09:00-09:45 の 4 本: Open=最初, Close=最後, High=max, Low=min, Vol=sum
    assert out["Open"].iloc[0] == 0.0
    assert out["Close"].iloc[0] == 3.5
    assert out["High"].iloc[0] == 4.0
    assert out["Low"].iloc[0] == -1.0
    assert out["Volume"].iloc[0] == 4.0


def test_downsample_below_base_raises():
    df = _df_15m()
    with pytest.raises(ValueError):
        resample_ohlcv(df, "15m", base_interval="1h")  # 1h 基底から 15m は作れない


def test_unsupported_base_interval_raises():
    df = _df_15m()
    with pytest.raises(ValueError):
        resample_ohlcv(df, "1h", base_interval="99m")


def test_default_base_is_1h_backward_compat():
    idx = pd.date_range("2026-07-01 00:00", periods=8, freq="1h")
    df = pd.DataFrame(
        {"Open": range(8), "High": range(8), "Low": range(8),
         "Close": range(8), "Volume": [1.0] * 8},
        index=idx,
    ).astype(float)
    out = resample_ohlcv(df, "4h")
    assert len(out) == 2
