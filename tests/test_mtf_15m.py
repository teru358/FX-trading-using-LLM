"""MTF の 15m 対応 (spec S-4b / codex High#1): bars_per_day と基底足伝搬。"""
from src.data.mtf import _bars_per_day_for_interval


def test_bars_per_day_15m():
    assert _bars_per_day_for_interval("15m") == 96


def test_bars_per_day_30m():
    assert _bars_per_day_for_interval("30m") == 48


def test_bars_per_day_existing_unchanged():
    assert _bars_per_day_for_interval("1d") == 1
    assert _bars_per_day_for_interval("1h") == 24
    assert _bars_per_day_for_interval("4h") == 6


def test_compute_mtf_summaries_from_15m_base():
    """15m 基底で short=15m / medium=1h の両 summary が生成される (実経路、codex Med#2)。"""
    import pandas as pd

    from src.config.schema import AnalysisConfig
    from src.data.mtf import compute_mtf_summaries

    # 4 日分の 15m OHLCV (384 本) — 各 TF の最低本数 (20) を満たす
    n = 384
    idx = pd.date_range("2026-06-27", periods=n, freq="15min")
    close = [150.0 + (i % 10) * 0.01 for i in range(n)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + 0.02 for c in close],
         "Low": [c - 0.02 for c in close], "Close": close, "Volume": [1.0] * n},
        index=idx,
    )
    timeframes = {
        "long":   {"lookback_days": 15, "interval": "4h",  "enabled": False},
        "medium": {"lookback_days": 4,  "interval": "1h",  "enabled": True},
        "short":  {"lookback_days": 1,  "interval": "15m", "enabled": True},
    }
    summaries = compute_mtf_summaries(
        df, AnalysisConfig(), timeframes, base_interval="15m"
    )
    assert "short" in summaries
    assert "medium" in summaries
