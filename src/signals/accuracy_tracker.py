"""Forecast accuracy auto-feedback ユーティリティ。

ForecastStore に蓄積された直近 N 時間の予測 (reviewed=1) について、
predicted_direction と latest_price_delta の符号一致率 (= hit 率) を計算する。
signal_combiner がこの値を読んで confidence/action にペナルティを掛ける。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.analysis_store import ForecastStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccuracyResult:
    """直近の予測精度集計結果。"""
    accuracy: float        # 0.0–1.0
    sample_count: int      # reviewed 済みレコード数
    correct_count: int     # 方向が一致したレコード数


def compute_recent_accuracy(
    forecast_store: "ForecastStore",
    pair: str,
    hours: int = 24,
) -> AccuracyResult | None:
    """直近 N 時間の予測 hit 率を計算する。

    対象: ``reviewed == 1`` かつ ``latest_price_delta is not None`` のレコード。
    sample が 0 の場合は ``None`` を返し、呼び出し側で「データ不足」として扱う。

    Args:
        forecast_store: 予測レコード DB アクセサ
        pair: 通貨ペアシンボル (e.g. "USDJPY=X")
        hours: 集計対象とする直近時間幅

    Returns:
        AccuracyResult | None
    """
    records = forecast_store.get_recent_forecasts(pair, hours=hours)
    reviewed = [
        r for r in records
        if r.reviewed == 1 and r.latest_price_delta is not None
    ]
    if not reviewed:
        return None

    correct = 0
    for r in reviewed:
        delta = r.latest_price_delta
        if r.predicted_direction == "bullish" and delta > 0:
            correct += 1
        elif r.predicted_direction == "bearish" and delta < 0:
            correct += 1

    total = len(reviewed)
    return AccuracyResult(
        accuracy=correct / total,
        sample_count=total,
        correct_count=correct,
    )
