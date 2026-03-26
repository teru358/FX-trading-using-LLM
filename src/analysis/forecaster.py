"""予測サイクル: 総合分析予測の検証ロジック（LLM不使用）。

A: ATR proxy による有意性フィルター
B: 8h検証ウィンドウ（呼び出し側で制御）
C: 高確信度のみ予測生成（呼び出し側で制御）
D: 事実文字列として蓄積（LLM不使用）
"""

from __future__ import annotations

from datetime import datetime

from src.data.analysis_store import _ForecastRecord


def build_forecast_review(
    pair: str,
    forecast: _ForecastRecord,
    current_price: float,
    review_ts: datetime,
    significance_atr_ratio: float = 0.30,
) -> tuple[str, bool]:
    """予測と実際の結果を比較し、事実文字列と有意性フラグを返す。

    Args:
        pair: 通貨ペアシンボル
        forecast: 検証対象の予測レコード
        current_price: 現在価格
        review_ts: 検証時刻
        significance_atr_ratio: ATR proxy に対する有意性閾値の比率

    Returns:
        (review_text, is_significant)
        is_significant=False の場合は RAG に蓄積しない（判定不能）
    """
    delta = current_price - forecast.current_price

    if delta > 0:
        actual_direction = "bullish"
    elif delta < 0:
        actual_direction = "bearish"
    else:
        actual_direction = "neutral"

    correct = (forecast.predicted_direction == actual_direction)

    # A: ATR proxy による有意性判定（stop_loss が記録されている場合）
    if forecast.stop_loss and forecast.stop_loss > 0 and forecast.current_price > 0:
        atr_proxy = abs(forecast.current_price - forecast.stop_loss)
        threshold = atr_proxy * significance_atr_ratio
        significant = abs(delta) >= threshold
    else:
        # フォールバック: 0.05% 以上の動きを有意とする
        significant = (
            abs(delta / forecast.current_price) >= 0.0005
            if forecast.current_price > 0
            else False
        )

    elapsed_h = (review_ts - forecast.forecast_ts).total_seconds() / 3600
    correct_mark = "✓" if correct else "✗"
    sig_label = "[significant]" if significant else "[inconclusive]"

    review_text = (
        f"[Forecast {pair} "
        f"{forecast.forecast_ts.strftime('%m-%d %H:%M')}→{review_ts.strftime('%m-%d %H:%M')}"
        f"({elapsed_h:.0f}h)] "
        f"{forecast.predicted_direction.upper()} score={forecast.combined_score:+.3f} "
        f"conf={forecast.confidence:.2f} "
        f"→ actual {actual_direction} {delta:+.5f} "
        f"{correct_mark} {sig_label}"
    )

    lesson = (
        f"{'CORRECT' if correct else 'INCORRECT'} {forecast.predicted_direction} call — "
        f"actual {actual_direction} {delta:+.5f} ({'significant move' if significant else 'weak move'})"
    )

    return review_text, lesson, significant
