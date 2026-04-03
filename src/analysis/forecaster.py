"""予測サイクル / HOLD検証: 事実ベース検証ロジック（LLM不使用）。

A: ATR proxy による有意性フィルター
B: 8h検証ウィンドウ（呼び出し側で制御）
C: 高確信度のみ予測生成（呼び出し側で制御）
D: 事実文字列として蓄積（LLM不使用）
"""

from __future__ import annotations

from datetime import datetime

from src.data.analysis_store import _ForecastRecord, _HoldDecisionRecord


def build_forecast_review(
    pair: str,
    forecast: _ForecastRecord,
    current_price: float,
    review_ts: datetime,
    significance_atr_ratio: float = 0.30,
    atr_value: float | None = None,
) -> tuple[str, bool]:
    """予測と実際の結果を比較し、事実文字列と有意性フラグを返す。

    Args:
        pair: 通貨ペアシンボル
        forecast: 検証対象の予測レコード
        current_price: 現在価格
        review_ts: 検証時刻
        significance_atr_ratio: ATR proxy に対する有意性閾値の比率
        atr_value: 本物のATR(14)値。指定時はSL距離ベースの代理値より優先。

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

    # A: ATR による有意性判定（本物のATR優先、フォールバックでSL距離を代理値として使用）
    if atr_value and atr_value > 0:
        threshold = atr_value * significance_atr_ratio
        significant = abs(delta) >= threshold
    elif forecast.stop_loss and forecast.stop_loss > 0 and forecast.current_price > 0:
        atr_proxy = abs(forecast.current_price - forecast.stop_loss)
        threshold = atr_proxy * significance_atr_ratio
        significant = abs(delta) >= threshold
    else:
        significant = (
            abs(delta / forecast.current_price) >= 0.0005
            if forecast.current_price > 0
            else False
        )

    elapsed_h = (review_ts - forecast.forecast_ts).total_seconds() / 3600
    correct_mark = "✓" if correct else "✗"
    sig_label = "[significant]" if significant else "[inconclusive]"

    macro_note = ""
    if getattr(forecast, "macro_context_at_forecast", None):
        macro_note = f" | macro_at_entry=[{forecast.macro_context_at_forecast[:120]}]"

    review_text = (
        f"[Forecast {pair} "
        f"{forecast.forecast_ts.strftime('%m-%d %H:%M')}→{review_ts.strftime('%m-%d %H:%M')}"
        f"({elapsed_h:.0f}h)] "
        f"{forecast.predicted_direction.upper()} score={forecast.combined_score:+.3f} "
        f"conf={forecast.confidence:.2f} "
        f"→ actual {actual_direction} {delta:+.5f} "
        f"{correct_mark} {sig_label}"
        f"{macro_note}"
    )

    lesson = (
        f"{'CORRECT' if correct else 'INCORRECT'} {forecast.predicted_direction} call — "
        f"actual {actual_direction} {delta:+.5f} ({'significant move' if significant else 'weak move'})"
    )

    return review_text, lesson, significant


def build_forecast_review_summary(
    pair: str,
    forecasts: list[_ForecastRecord],
    current_price: float,
    review_ts: datetime,
    significance_atr_ratio: float = 0.30,
    atr_value: float | None = None,
) -> tuple[str, str, bool]:
    """直近24hの複数予測を一括検証し、集計サマリーと有意性フラグを返す。

    Args:
        pair: 通貨ペアシンボル
        forecasts: 検証対象の予測レコードリスト（古い順）
        current_price: 現在価格
        review_ts: 検証時刻
        significance_atr_ratio: ATR proxy に対する有意性閾値の比率
        atr_value: 本物のATR(14)値。指定時はSL距離ベースの代理値より優先。

    Returns:
        (summary_text, lesson, has_significant)
        has_significant=False の場合は有意な動きがなく RAG に蓄積しない
    """
    if not forecasts:
        return "", "", False

    results = []
    for fc in forecasts:
        _, lesson, significant = build_forecast_review(
            pair=pair,
            forecast=fc,
            current_price=current_price,
            review_ts=review_ts,
            significance_atr_ratio=significance_atr_ratio,
            atr_value=atr_value,
        )
        delta = current_price - fc.current_price
        actual_direction = "bullish" if delta > 0 else ("bearish" if delta < 0 else "neutral")
        correct = fc.predicted_direction == actual_direction
        results.append({
            "fc": fc,
            "correct": correct,
            "significant": significant,
            "actual_direction": actual_direction,
            "delta": delta,
            "lesson": lesson,
        })

    total = len(results)
    significant_results = [r for r in results if r["significant"]]
    correct_significant = [r for r in significant_results if r["correct"]]
    incorrect_significant = [r for r in significant_results if not r["correct"]]
    inconclusive = [r for r in results if not r["significant"]]

    has_significant = len(significant_results) > 0
    accuracy_pct = len(correct_significant) / len(significant_results) * 100 if significant_results else 0.0

    avg_score = sum(r["fc"].combined_score for r in results) / total
    avg_conf = sum(r["fc"].confidence for r in results) / total

    direction_counts: dict[str, int] = {}
    for r in results:
        d = r["fc"].predicted_direction
        direction_counts[d] = direction_counts.get(d, 0) + 1
    dir_summary = " ".join(f"{d}×{c}" for d, c in direction_counts.items())

    oldest_ts = forecasts[0].forecast_ts
    newest_ts = forecasts[-1].forecast_ts

    summary_text = (
        f"[Forecast Summary {pair} "
        f"{oldest_ts.strftime('%m-%d %H:%M')}→{review_ts.strftime('%m-%d %H:%M')}] "
        f"Reviewed: {total} | Correct: {len(correct_significant)} | "
        f"Incorrect: {len(incorrect_significant)} | Inconclusive: {len(inconclusive)} | "
        f"Directions: {dir_summary} | "
        f"avg_score={avg_score:+.3f} avg_conf={avg_conf:.2f} | "
        f"Accuracy: {accuracy_pct:.0f}% (significant only)"
    )

    if correct_significant:
        lesson_parts = [f"CORRECT: {r['fc'].predicted_direction} (score={r['fc'].combined_score:+.3f} actual={r['actual_direction']})" for r in correct_significant]
    else:
        lesson_parts = []
    if incorrect_significant:
        lesson_parts += [f"INCORRECT: {r['fc'].predicted_direction} (score={r['fc'].combined_score:+.3f} actual={r['actual_direction']})" for r in incorrect_significant]

    lesson = f"24h summary for {pair}: " + "; ".join(lesson_parts) if lesson_parts else f"24h summary for {pair}: no significant moves"

    return summary_text, lesson, has_significant


def build_hold_review(
    pair: str,
    hold: _HoldDecisionRecord,
    current_price: float,
    review_ts: datetime,
    significance_atr_ratio: float = 0.30,
    atr_value: float | None = None,
) -> tuple[str, str, bool]:
    """HOLD判断と実際の価格変動を比較し、事実文字列と蓄積要否フラグを返す。

    Args:
        pair: 通貨ペアシンボル
        hold: 検証対象のHOLD記録
        current_price: 現在価格
        review_ts: 検証時刻
        significance_atr_ratio: ATR proxy に対する有意性閾値の比率
        atr_value: 本物のATR(14)値。指定時はSL距離ベースの代理値より優先。

    Returns:
        (review_text, lesson, worth_storing)
        worth_storing=False の場合は価格変動が小さく判定不能
    """
    delta = current_price - hold.current_price
    delta_pct = delta / hold.current_price * 100 if hold.current_price > 0 else 0.0

    signal_bullish = hold.signal_score > 0
    signal_bearish = hold.signal_score < 0
    price_went_up = delta > 0
    price_went_down = delta < 0

    direction_correct = (signal_bullish and price_went_up) or (signal_bearish and price_went_down)

    # A: ATR による有意性判定（本物のATR優先）
    if atr_value and atr_value > 0:
        significant = abs(delta) >= atr_value * significance_atr_ratio
    elif hold.stop_loss and hold.stop_loss > 0 and hold.current_price > 0:
        atr_proxy = abs(hold.current_price - hold.stop_loss)
        significant = abs(delta) >= atr_proxy * significance_atr_ratio
    else:
        significant = (
            abs(delta / hold.current_price) >= 0.0005
            if hold.current_price > 0
            else False
        )

    elapsed_h = (review_ts - hold.decision_ts).total_seconds() / 3600

    if not significant:
        verdict = "HOLD_CORRECT(no_move)"
        correct_mark = "–"
    elif direction_correct:
        verdict = "SHOULD_HAVE_ENTERED"
        correct_mark = "✗"
    else:
        verdict = "HOLD_CORRECT(wrong_dir)"
        correct_mark = "✓"

    review_text = (
        f"[Hold {pair} "
        f"{hold.decision_ts.strftime('%m-%d %H:%M')}→{review_ts.strftime('%m-%d %H:%M')}"
        f"({elapsed_h:.0f}h)] "
        f"HOLD {hold.predicted_direction} score={hold.signal_score:+.3f} "
        f"conf={hold.confidence:.2f} "
        f"→ {delta:+.5f} ({delta_pct:+.2f}%) "
        f"{correct_mark} {verdict}"
    )

    lesson = (
        f"{verdict}: {hold.predicted_direction} bias (score={hold.signal_score:+.3f}) "
        f"on {pair} — price moved {delta:+.5f} ({delta_pct:+.2f}%) over {elapsed_h:.0f}h"
    )

    return review_text, lesson, significant
