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


def test_compute_mtf_summaries_default_1h_base():
    """既定 base_interval="1h" の従来経路が生きていること (回帰ガード)。"""
    import pandas as pd

    from src.config.schema import AnalysisConfig
    from src.data.mtf import compute_mtf_summaries

    n = 96  # 4 日分の 1h
    idx = pd.date_range("2026-06-27", periods=n, freq="1h")
    close = [150.0 + (i % 10) * 0.01 for i in range(n)]
    df = pd.DataFrame(
        {"Open": close, "High": [c + 0.02 for c in close],
         "Low": [c - 0.02 for c in close], "Close": close, "Volume": [1.0] * n},
        index=idx,
    )
    timeframes = {
        "long":   {"lookback_days": 15, "interval": "4h", "enabled": True},
        "medium": {"lookback_days": 4,  "interval": "4h", "enabled": False},
        "short":  {"lookback_days": 2,  "interval": "1h", "enabled": True},
    }
    summaries = compute_mtf_summaries(df, AnalysisConfig(), timeframes)  # kwarg なし
    assert "short" in summaries
    assert "long" in summaries


def test_base_interval_is_threaded_to_resample(monkeypatch):
    """medium(1h) へ base_interval="15m" が渡ることを spy で直接検証。"""
    import pandas as pd

    import src.data.mtf as mtf_mod
    from src.config.schema import AnalysisConfig
    from src.data.resample import resample_ohlcv as real_resample

    calls = []

    def _spy(df, interval, base_interval="1h"):
        calls.append((interval, base_interval))
        return real_resample(df, interval, base_interval=base_interval)

    monkeypatch.setattr(mtf_mod, "resample_ohlcv", _spy)

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
    mtf_mod.compute_mtf_summaries(df, AnalysisConfig(), timeframes, base_interval="15m")
    assert ("1h", "15m") in calls
    assert ("15m", "15m") in calls
