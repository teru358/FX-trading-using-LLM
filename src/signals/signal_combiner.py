from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from src.analysis.news_analyzer import NewsSentiment
from src.analysis.price_analyzer import PriceAnalysis
from src.config.schema import ForecastAccuracyFeedbackConfig
from src.signals.accuracy_tracker import AccuracyResult
from src.utils.clock import db_now

logger = logging.getLogger(__name__)

# accuracy_provider 型: pair symbol → AccuracyResult or None
AccuracyProvider = Callable[[str], AccuracyResult | None]


@dataclass
class TradeSignal:
    pair: str
    action: str             # "buy" | "sell" | "hold"
    predicted_direction: str  # "bullish" | "bearish" | "neutral" — HOLDでも常に設定
    combined_score: float
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    signal_reason: str
    detail_reason: str          # 通知用: ニュース/テクニカル内訳 + 判定ロジック
    news: NewsSentiment
    price: PriceAnalysis
    generated_at: datetime


def combine_signals(
    news: NewsSentiment,
    price: PriceAnalysis,
    current_price: float,
    pair_cfg,
    account_balance: float,
    risk_per_trade: float,
    confidence_threshold: float,
    news_weight: float = 0.40,
    price_weight: float = 0.60,
    signal_deadband: float = 0.15,
    min_lot_size: float = 1000.0,
    lot_unit: float = 1000.0,
    tv_summary=None,  # TVSummary | None (矛盾検出用)
    tv_conflict_dampen: float = 0.7,  # TV方向矛盾時のconfidence減衰率
    min_rr_ratio: float = 0.0,        # 未使用 (ATR後に _apply_atr_sltp_to_signal で判定)
    accuracy_provider: AccuracyProvider | None = None,
    accuracy_config: ForecastAccuracyFeedbackConfig | None = None,
) -> TradeSignal:
    # Dynamic weight adjustment based on news confidence
    if news.confidence >= 0.80:
        effective_news_weight = min(news_weight + 0.10, 0.50)
        effective_price_weight = 1.0 - effective_news_weight
    elif news.confidence <= 0.30:
        effective_news_weight = max(news_weight - 0.10, 0.20)
        effective_price_weight = 1.0 - effective_news_weight
    else:
        effective_news_weight = news_weight
        effective_price_weight = price_weight

    # Weighted score
    combined_score = (news.sentiment_score * effective_news_weight) + (price.bias_score * effective_price_weight)

    # Alignment check: penalise conflicting signals (scaled by weaker confidence)
    # ニュース重み削減後はテクニカル主導のため、ペナルティを緩和 (0.5→0.3)
    alignment = news.sentiment_score * price.bias_score
    if alignment >= 0:
        conflict_penalty = 1.0
    else:
        weaker_conf = min(news.confidence, price.confidence)
        conflict_penalty = 1.0 - (0.3 * weaker_conf)
    combined_score *= conflict_penalty

    # Combined confidence
    combined_confidence = (
        (news.confidence * effective_news_weight + price.confidence * effective_price_weight) * conflict_penalty
    )

    # TradingView テクニカルサマリーとの矛盾検出
    tv_conflict = False
    if tv_summary is not None and tv_summary.direction != "neutral":
        price_dir = "long" if price.bias_score > 0.05 else "short" if price.bias_score < -0.05 else "neutral"
        if price_dir != "neutral" and tv_summary.direction != price_dir:
            combined_confidence *= tv_conflict_dampen
            combined_score *= tv_conflict_dampen
            tv_conflict = True
            logger.info(
                f"[SIGNAL] {pair_cfg.display_name}: TV conflict — "
                f"price={price_dir} TV={tv_summary.direction} "
                f"(buy={tv_summary.buy}/sell={tv_summary.sell}) "
                f"conf×{tv_conflict_dampen}"
            )

    # Deadband: confidence 連動 + market_regime 連動
    # Step 1: confidence による基本調整
    if combined_confidence >= 0.80:
        effective_deadband = signal_deadband * 0.67  # 0.15 → 0.10
    elif combined_confidence < 0.60:
        effective_deadband = signal_deadband * 1.33  # 0.15 → 0.20
    else:
        effective_deadband = signal_deadband

    # Step 2: market_regime による追加調整
    # trending → トレンドに乗りやすく (×0.8)、ranging → レンジノイズ回避 (×1.2)
    regime = getattr(price, "market_regime", "unknown")
    if regime == "trending":
        effective_deadband *= 0.8
    elif regime == "ranging":
        effective_deadband *= 1.2

    # ── Forecast accuracy auto-feedback ──
    # 直近サイクルの予測精度が低いペアでは confidence をペナルティ、
    # 著しく低い場合は action="hold" を強制 (誤った方向への新規 entry を防ぐ)。
    accuracy_note = ""
    accuracy_force_hold = False
    if (
        accuracy_provider is not None
        and accuracy_config is not None
        and accuracy_config.enabled
    ):
        try:
            acc = accuracy_provider(pair_cfg.symbol)
        except Exception as e:  # noqa: BLE001 - provider 失敗で signal は止めない
            logger.warning(f"[SIGNAL] {pair_cfg.display_name}: accuracy_provider raised: {e}")
            acc = None
        if acc is not None and acc.sample_count >= accuracy_config.min_samples:
            if acc.accuracy < accuracy_config.hard_threshold:
                accuracy_force_hold = True
                accuracy_note = (
                    f"⚠ forecast accuracy {acc.accuracy:.0%} "
                    f"({acc.correct_count}/{acc.sample_count}) "
                    f"< hard {accuracy_config.hard_threshold:.0%} → forced HOLD"
                )
                logger.warning(
                    f"[SIGNAL] {pair_cfg.display_name}: forced HOLD by accuracy "
                    f"{acc.accuracy:.0%} ({acc.correct_count}/{acc.sample_count})"
                )
            elif acc.accuracy < accuracy_config.soft_threshold:
                old_conf = combined_confidence
                combined_confidence *= accuracy_config.confidence_penalty
                accuracy_note = (
                    f"⚠ forecast accuracy {acc.accuracy:.0%} "
                    f"({acc.correct_count}/{acc.sample_count}) "
                    f"→ conf×{accuracy_config.confidence_penalty} "
                    f"({old_conf:.2f}→{combined_confidence:.2f})"
                )
                logger.info(
                    f"[SIGNAL] {pair_cfg.display_name}: accuracy penalty "
                    f"{old_conf:.2f}→{combined_confidence:.2f} (acc={acc.accuracy:.0%})"
                )

    if accuracy_force_hold:
        action = "hold"
        reason = "forecast accuracy below hard_threshold"
    elif combined_confidence < confidence_threshold:
        action = "hold"
        reason = f"confidence too low ({combined_confidence:.2f} < {confidence_threshold})"
    elif combined_score > effective_deadband:
        action = "buy"
        reason = f"score={combined_score:+.3f} conf={combined_confidence:.2f}"
    elif combined_score < -effective_deadband:
        action = "sell"
        reason = f"score={combined_score:+.3f} conf={combined_confidence:.2f}"
    else:
        action = "hold"
        reason = f"score in deadband ({combined_score:+.3f}, db={effective_deadband:.3f})"

    # R:R チェックは ATR SL/TP 算出後に実施 (_apply_atr_sltp_to_signal 内)
    # LLM の SL/TP は信頼性が低いため、combine_signals 段階では判定しない

    if alignment < 0:
        reason += " [NEWS/PRICE conflict]"

    # 方向予測（action に関わらず常に設定）
    if combined_score > 0.05:
        predicted_direction = "bullish"
    elif combined_score < -0.05:
        predicted_direction = "bearish"
    else:
        predicted_direction = "neutral"

    # Entry price: 成行約定前提のため current_price を使用
    # LLM の entry_zone は理想的なエントリーゾーンだが、ペーパートレードでは
    # 指値注文ができないため current_price で約定する。entry_zone は reasoning 参考用。
    entry_price = current_price

    # SL/TP と position_size は ATR ベースで後から算出される (_apply_atr_sltp_to_signal)
    # ここではプレースホルダーを設定し、ATR 未通過時のフォールバックとする
    stop_loss = price.stop_loss
    take_profit = price.take_profit
    position_size = min_lot_size  # ATR で再計算される前のフォールバック値

    # 通知用の詳細理由を構築
    news_themes = ", ".join(news.key_themes[:3]) if news.key_themes else "N/A"
    detail_lines = [
        f"ニュース: {news.sentiment_score:+.2f} ({news.confidence:.0%}) — {news_themes}",
        f"テクニカル(ルール): {price.bias_score:+.2f} ({price.confidence:.0%}) — {price.direction_bias}",
    ]
    if tv_summary is not None:
        tv_mark = "⚠" if tv_conflict else "✓"
        detail_lines.append(
            f"TV consensus: {tv_summary.recommendation} "
            f"(buy={tv_summary.buy}/sell={tv_summary.sell}/neutral={tv_summary.neutral}) {tv_mark}"
        )
    detail_lines.append(f"合成: {combined_score:+.3f} → {action.upper()} ({reason})")
    if accuracy_note:
        detail_lines.append(accuracy_note)
    detail_reason = "\n".join(detail_lines)

    signal = TradeSignal(
        pair=pair_cfg.symbol,
        action=action,
        predicted_direction=predicted_direction,
        combined_score=round(combined_score, 4),
        confidence=round(combined_confidence, 4),
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        position_size=position_size,
        signal_reason=reason,
        detail_reason=detail_reason,
        news=news,
        price=price,
        generated_at=db_now(),
    )

    conflict_str = " [CONFLICT]" if alignment < 0 else ""
    direction_str = f" → {predicted_direction}" if action == "hold" else ""
    logger.info(
        f"[SIGNAL] {pair_cfg.display_name}: {action.upper()}{direction_str} | "
        f"score={combined_score:+.3f} "
        f"(news={news.sentiment_score:+.2f}×{effective_news_weight:.2f} + tech={price.bias_score:+.2f}×{effective_price_weight:.2f}){conflict_str} | "
        f"conf={combined_confidence:.2f} | {reason}"
    )
    if action != "hold":
        logger.info(
            f"[SIGNAL] {pair_cfg.display_name}: entry={entry_price:.5f} "
            f"(SL/TP/size は ATR 後に算出)"
        )
    return signal


def _calculate_position_size(
    balance: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    pip_value: float,
    min_lot_size: float = 1000.0,
    lot_unit: float = 1000.0,
) -> float:
    risk_amount = balance * risk_pct
    price_distance = abs(entry - stop_loss)
    if price_distance < pip_value:
        # Fallback: use 1% of entry as minimum stop distance
        price_distance = entry * 0.01
    pips_at_risk = price_distance / pip_value
    lot_size = risk_amount / pips_at_risk
    lot_size = max(min_lot_size, round(lot_size / lot_unit) * lot_unit)
    return lot_size
