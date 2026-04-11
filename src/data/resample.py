"""OHLCV DataFrame のタイムフレーム変換ユーティリティ。

1h 足の DataFrame を 4h / 8h / 1d などにリサンプルする。yfinance / TwelveData
から取得した 1h データを基点に、MTF (Multi-Timeframe) 分析で各時間軸用の
OHLCV を生成する。
"""
from __future__ import annotations

import pandas as pd

# pandas の resample rule との対応。"1h" は恒等 (リサンプル不要)。
_RULE_MAP: dict[str, str] = {
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1D",
    "1w": "1W",
}


def resample_ohlcv(df_1h: pd.DataFrame, interval: str) -> pd.DataFrame:
    """1h OHLCV を指定タイムフレームへ集約する。

    Args:
        df_1h: 1h 足の OHLCV DataFrame。index は DatetimeIndex。
               列は "Open", "High", "Low", "Close", "Volume"。
        interval: 目的タイムフレーム。"1h", "4h", "8h", "1d" などをサポート。

    Returns:
        リサンプル後の DataFrame。同じ 5 列 + DatetimeIndex。
        NaN バケットは除去済み。

    Note:
        interval が "1h" の場合は copy を返すだけ (恒等)。
    """
    if interval not in _RULE_MAP:
        raise ValueError(
            f"Unsupported interval: {interval!r}. "
            f"Supported: {sorted(_RULE_MAP.keys())}"
        )

    if interval == "1h":
        return df_1h.copy()

    rule = _RULE_MAP[interval]
    resampled = df_1h.resample(rule).agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    # 空バケット (データがない時間帯) を除去
    return resampled.dropna(subset=["Open", "High", "Low", "Close"])
