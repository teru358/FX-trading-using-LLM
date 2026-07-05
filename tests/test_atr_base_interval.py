"""ATR の基底足対応 (codex High#1): 15m 基底 + atr_timeframe=1h で 1h ATR になること。"""
import pandas as pd
from types import SimpleNamespace

from src.cycles._helpers import _compute_atr_from_price_data


def _pd15(n=200, amp=0.1):
    idx = pd.date_range("2026-07-01", periods=n, freq="15min")
    close = [150.0 + (i % 8) * amp for i in range(n)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + amp for c in close],
         "Low": [c - amp for c in close], "Close": close, "Volume": [1.0] * n},
        index=idx,
    )
    return SimpleNamespace(df=df)


def test_1h_atr_from_15m_base_resamples():
    """base=15m のとき resample_tf='1h' は必ずリサンプルされ、15m ATR より大きくなる。"""
    pd15 = _pd15()
    atr_15m = _compute_atr_from_price_data(pd15, resample_tf="", base_interval="15m")
    atr_1h = _compute_atr_from_price_data(pd15, resample_tf="1h", base_interval="15m")
    assert atr_15m is not None and atr_1h is not None
    assert atr_1h > atr_15m  # 1h バーは 4 本分のレンジを含むため必ず広い


def test_default_base_1h_identity_unchanged():
    """従来挙動: base=1h (既定) で resample_tf='1h' はリサンプルなし (挙動不変)。"""
    idx = pd.date_range("2026-07-01", periods=200, freq="1h")
    close = [150.0 + (i % 8) * 0.1 for i in range(200)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + 0.1 for c in close],
         "Low": [c - 0.1 for c in close], "Close": close, "Volume": [1.0] * 200},
        index=idx,
    )
    pdata = SimpleNamespace(df=df)
    assert _compute_atr_from_price_data(pdata, resample_tf="1h") == \
           _compute_atr_from_price_data(pdata, resample_tf="")
