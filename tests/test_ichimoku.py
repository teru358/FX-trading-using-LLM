"""一目均衡表 (_compute_ichimoku) の判定ロジックテスト。

特に遅行スパン (chikou span) の bullish/bearish 方向を回帰ガードする。
定義:
  - 遅行スパン = 現在の終値を 26 本前にプロットしたもの
  - 判定対象 = 「今日の終値 (= 遅行スパンの値)」 vs 「26 本前の実際の終値」
  - bullish: 今日の終値 > 26 本前の終値 (現価格が過去より上)
  - bearish: 今日の終値 < 26 本前の終値
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.data.indicators import _compute_ichimoku


def _make_df(closes: list[float]) -> pd.DataFrame:
    """終値列から OHLCV DataFrame を構築する。High/Low/Open/Volume は
    判定に影響しない範囲でダミー。"""
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h")
    return pd.DataFrame(
        {
            "Open":   closes,
            "High":   [c + 0.5 for c in closes],
            "Low":    [c - 0.5 for c in closes],
            "Close":  closes,
            "Volume": [1000] * len(closes),
        },
        index=idx,
    )


def _linear(start: float, end: float, n: int) -> list[float]:
    step = (end - start) / (n - 1)
    return [start + step * i for i in range(n)]


# ── chikou span 方向判定 ─────────────────────────────────────


def test_chikou_bullish_when_today_close_above_26_bars_ago():
    """今日の終値 > 26 本前の終値 → bullish 方向にカウントされる。

    遅行スパンの値 (= 今日の終値) が過去 (= 26 本前の終値) より上 → 強気。
    """
    # 60 本の緩やかな上昇トレンド: 最後の終値は 26 本前より確実に高い
    closes = _linear(100.0, 120.0, 60)
    df = _make_df(closes)

    # データ点 -27 (= 26 本前の終値) と -1 (= 今日の終値) を確認
    assert closes[-1] > closes[-27], "テストデータ前提: 今日 > 26 本前"

    result = _compute_ichimoku(df)
    # chikou_span フィールドは「26 本前の終値」を返す設計
    assert result["chikou_span"] == pytest.approx(closes[-27])
    # 上昇トレンド + 雲上 + 転換 > 基準 → strong_bullish 判定
    assert result["ichimoku_signal"] in ("strong_bullish", "bullish")


def test_chikou_bearish_when_today_close_below_26_bars_ago():
    """今日の終値 < 26 本前の終値 → bearish 方向にカウントされる。"""
    # 60 本の緩やかな下降トレンド
    closes = _linear(120.0, 100.0, 60)
    df = _make_df(closes)
    assert closes[-1] < closes[-27]

    result = _compute_ichimoku(df)
    assert result["chikou_span"] == pytest.approx(closes[-27])
    assert result["ichimoku_signal"] in ("strong_bearish", "bearish")
