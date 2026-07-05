"""OHLCV DataFrame のタイムフレーム変換ユーティリティ。

基底足 (15m / 30m / 1h など) の DataFrame を 4h / 8h / 1d などの
より粗い時間軸にリサンプルする。yfinance / TwelveData から取得した
基底足データを基点に、MTF (Multi-Timeframe) 分析で各時間軸用の
OHLCV を生成する。
"""
from __future__ import annotations

import pandas as pd

# pandas の resample rule との対応。分足は "min" 単位 (pandas 2.x で "T" は非推奨)。
_RULE_MAP: dict[str, str] = {
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1D",
    "1w": "1W",
}

# interval → 分数 (粒度比較用)。_RULE_MAP と同じキーを持つ。
_INTERVAL_MINUTES: dict[str, int] = {
    "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240,
    "6h": 360, "8h": 480, "12h": 720, "1d": 1440, "1w": 10080,
}

# 2 つの map は同一キー集合を維持する (drift すると ValueError 契約が KeyError に化ける)
assert _RULE_MAP.keys() == _INTERVAL_MINUTES.keys()


def resample_ohlcv(
    df_base: pd.DataFrame, interval: str, base_interval: str = "1h"
) -> pd.DataFrame:
    """基底足 OHLCV を指定タイムフレームへ集約する。

    Args:
        df_base: 基底足 (15m / 30m / 1h など) の OHLCV DataFrame。
                 index は DatetimeIndex。列は "Open", "High", "Low", "Close", "Volume"。
        interval: 目的タイムフレーム。"1h", "4h", "8h", "1d" などをサポート。
        base_interval: 入力データの基底足。"15m", "30m", "1h" などで指定。
                       デフォルトは "1h" (後方互換)。

    Returns:
        リサンプル後の DataFrame。同じ 5 列 + DatetimeIndex。
        NaN バケットは除去済み。

    Raises:
        ValueError: サポートされていないタイムフレーム、または
                    base_interval より粗い interval を指定された場合。

    Note:
        interval が base_interval と同じ場合は copy を返すだけ (恒等)。
    """
    # 入力検証
    if interval not in _RULE_MAP:
        raise ValueError(
            f"Unsupported interval: {interval!r}. "
            f"Supported: {sorted(_RULE_MAP.keys())}"
        )
    if base_interval not in _INTERVAL_MINUTES:
        raise ValueError(
            f"Unsupported base_interval: {base_interval!r}. "
            f"Supported: {sorted(_INTERVAL_MINUTES.keys())}"
        )
    if _INTERVAL_MINUTES[interval] < _INTERVAL_MINUTES[base_interval]:
        raise ValueError(
            f"cannot resample {base_interval} base down to finer {interval}"
        )

    # 恒等: 目的タイムフレーム = 基底足の場合
    if interval == base_interval:
        return df_base.copy()

    # リサンプル実行
    rule = _RULE_MAP[interval]
    resampled = df_base.resample(rule).agg(
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
