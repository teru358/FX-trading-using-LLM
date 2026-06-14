"""ポジション再評価ロジック。

Reversal Guard: 反転シグナルを即時決済の主経路にせず、原則リスク圧縮へ回す。
Time Stop: 日数/TP進捗ではなく、保有時間とMFE進捗で撤退判断する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


@dataclass
class ReviewDecision:
    """ポジション再評価の判断。

    action="close" のときだけ broker close を実行する。
    action="raise_sl" は pending protection target として保存する。
    """
    order_id: str
    pair: str
    close_reason: str  # "reversal" | "timeout" | "reversal_guard"
    detail: str
    action: str = "close"  # "close" | "raise_sl"
    target_sl: float | None = None


def _classify_layer1(
    pos: Order,
    signal: Optional[TradeSignal],
    holding_minutes: float,
    reversal_min_holding_minutes: int,
    reversal_confidence_min: float,
    reversal_score_threshold: float,
    timeout_only: bool,
) -> str:
    if timeout_only:
        return "skipped_timeout_only"
    if signal is None:
        return "no_signal"
    is_reversal = (
        (pos.direction == "buy" and signal.predicted_direction == "bearish")
        or (pos.direction == "sell" and signal.predicted_direction == "bullish")
    )
    if not is_reversal:
        return "not_reversed"
    if holding_minutes < reversal_min_holding_minutes:
        return "min_holding_not_met"
    if signal.confidence < reversal_confidence_min:
        return "low_confidence"
    if abs(signal.combined_score) < reversal_score_threshold:
        return "low_score"
    return "would_fire"


def _classify_time_stop(
    *,
    time_stop_enabled: bool,
    holding_hours: float,
    max_holding_hours: int,
    max_favorable_r: float,
    no_progress_hours: int,
    no_progress_min_mfe_r: float,
) -> str:
    if not time_stop_enabled:
        return "disabled"
    if holding_hours >= max_holding_hours:
        return "max_holding_would_fire"
    if holding_hours >= no_progress_hours and max_favorable_r < no_progress_min_mfe_r:
        return "no_progress_would_fire"
    if holding_hours < no_progress_hours:
        return "holding_below_no_progress"
    return "mfe_progress_ok"


def review_open_positions(
    open_positions: list[Order],
    signals_by_pair: dict[str, TradeSignal],
    current_prices: dict[str, float],
    *,
    timeout_only: bool = False,
    reversal_confidence_min: float = 0.70,
    reversal_score_threshold: float = 0.25,
    reversal_min_holding_minutes: int = 240,
    reversal_close_enabled: bool = True,
    reversal_raise_sl_to_breakeven: bool = True,
    time_stop_enabled: bool = False,
    max_holding_hours: int = 12,
    no_progress_hours: int = 4,
    no_progress_min_mfe_r: float = 0.2,
) -> list[ReviewDecision]:
    """オープンポジションを再評価し、closeまたはrisk compression判断を返す。

    timeout_only=True: halt 中などで signal 不要時に Time Stop のみ評価する。
    """
    decisions: list[ReviewDecision] = []

    for pos in open_positions:
        signal = signals_by_pair.get(pos.pair)
        price = current_prices.get(pos.pair)
        if price is None:
            continue

        if pos.direction == "buy":
            progress = price - pos.entry_price
        else:
            progress = pos.entry_price - price

        holding_hours = (db_now() - pos.opened_at).total_seconds() / 3600
        holding_minutes = holding_hours * 60

        fired = False

        # Reversal Guard: defaultは即時closeではなく、利益中のSL引き上げ。
        if not timeout_only and signal is not None:
            is_reversal = (
                (pos.direction == "buy" and signal.predicted_direction == "bearish")
                or (pos.direction == "sell" and signal.predicted_direction == "bullish")
            )
            if (
                is_reversal
                and holding_minutes >= reversal_min_holding_minutes
                and signal.confidence >= reversal_confidence_min
                and abs(signal.combined_score) >= reversal_score_threshold
            ):
                in_profit = progress > 0
                if not reversal_close_enabled and in_profit and reversal_raise_sl_to_breakeven:
                    logger.info(
                        f"[REVIEW] {pos.pair}: Reversal Guard — "
                        f"raise SL to breakeven (score={signal.combined_score:+.3f} "
                        f"conf={signal.confidence:.2f})"
                    )
                    decisions.append(ReviewDecision(
                        order_id=pos.order_id,
                        pair=pos.pair,
                        close_reason="reversal_guard",
                        detail=(
                            f"Reversal guard: {pos.direction} position vs "
                            f"{signal.predicted_direction} signal; raise SL to breakeven"
                        ),
                        action="raise_sl",
                        target_sl=round(pos.entry_price, 5),
                    ))
                    fired = True
                elif reversal_close_enabled:
                    logger.info(
                        f"[REVIEW] {pos.pair}: reversal close — "
                        f"{pos.direction} vs {signal.predicted_direction} "
                        f"(score={signal.combined_score:+.3f} conf={signal.confidence:.2f} "
                        f"holding={holding_minutes:.0f}min)"
                    )
                    decisions.append(ReviewDecision(
                        order_id=pos.order_id,
                        pair=pos.pair,
                        close_reason="reversal",
                        detail=(
                            f"Signal reversed: {pos.direction} position vs "
                            f"{signal.predicted_direction} signal "
                            f"(score={signal.combined_score:+.3f} conf={signal.confidence:.2f})"
                        ),
                    ))
                    fired = True
                else:
                    logger.info(
                        f"[REVIEW] {pos.pair}: Reversal Guard observed but no close "
                        f"(in_profit={in_profit})"
                    )

        # Time Stop: day/TP-progress fallbackは廃止。時間とMFEだけを見る。
        if not fired and time_stop_enabled and holding_hours >= max_holding_hours:
            logger.info(
                f"[REVIEW] {pos.pair}: Time Stop max holding — "
                f"{holding_hours:.1f}h >= {max_holding_hours}h"
            )
            decisions.append(ReviewDecision(
                order_id=pos.order_id,
                pair=pos.pair,
                close_reason="timeout",
                detail=(
                    f"Held {holding_hours:.1f}h >= max {max_holding_hours}h "
                    f"(mfe_r={pos.max_favorable_r:.2f})"
                ),
            ))
            fired = True

        if (
            not fired
            and time_stop_enabled
            and holding_hours >= no_progress_hours
            and pos.max_favorable_r < no_progress_min_mfe_r
        ):
            logger.info(
                f"[REVIEW] {pos.pair}: Time Stop no progress — "
                f"holding={holding_hours:.1f}h mfe_r={pos.max_favorable_r:.2f} "
                f"< {no_progress_min_mfe_r:.2f}"
            )
            decisions.append(ReviewDecision(
                order_id=pos.order_id,
                pair=pos.pair,
                close_reason="timeout",
                detail=(
                    f"Held {holding_hours:.1f}h with mfe_r={pos.max_favorable_r:.2f} "
                    f"< {no_progress_min_mfe_r:.2f} after {no_progress_hours}h"
                ),
            ))
            fired = True

        if not fired:
            score_str = f"{signal.combined_score:+.3f}" if signal else "N/A"
            conf_str = f"{signal.confidence:.2f}" if signal else "N/A"
            l1 = _classify_layer1(
                pos, signal, holding_minutes,
                reversal_min_holding_minutes, reversal_confidence_min,
                reversal_score_threshold, timeout_only,
            )
            time_stop = _classify_time_stop(
                time_stop_enabled=time_stop_enabled,
                holding_hours=holding_hours,
                max_holding_hours=max_holding_hours,
                max_favorable_r=pos.max_favorable_r,
                no_progress_hours=no_progress_hours,
                no_progress_min_mfe_r=no_progress_min_mfe_r,
            )
            logger.debug(
                f"[REVIEW] {pos.pair} eval: holding_h={holding_hours:.1f} "
                f"mfe_r={pos.max_favorable_r:.2f} score={score_str} conf={conf_str} "
                f"l1={l1} time_stop={time_stop}"
            )

    return decisions
