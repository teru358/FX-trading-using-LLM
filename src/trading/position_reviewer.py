"""ポジション再評価ロジック。

Reversal Guard: 反転シグナルを即時決済の主経路にせず、原則リスク圧縮へ回す。
Time Stop: 時間だけでは決済せず、保有時間・MFE進捗・現在シグナルの支持を合わせて撤退判断する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


TIMEOUT_CLOSE_REASONS = {"timeout", "timeout_no_progress", "timeout_stale_position"}


@dataclass
class ReviewDecision:
    """ポジション再評価の判断。

    action="close" のときだけ broker close を実行する。
    action="raise_sl" は pending protection target として保存する。
    """
    order_id: str
    pair: str
    close_reason: str
    detail: str
    action: str = "close"  # "close" | "raise_sl"
    target_sl: float | None = None


def is_timeout_close_reason(reason: str) -> bool:
    """L2 timeout family の close_reason かどうかを返す。"""
    return reason in TIMEOUT_CLOSE_REASONS or reason.startswith("timeout_")


def _position_direction(pos: Order) -> str:
    return "bullish" if pos.direction == "buy" else "bearish"


def _signal_supports_position(pos: Order, signal: Optional[TradeSignal]) -> bool:
    if signal is None:
        return False
    if signal.predicted_direction != _position_direction(pos):
        return False
    if signal.confidence < 0.55:
        return False
    if abs(signal.combined_score) < 0.05:
        return False
    return True


def _signal_is_weak_for_position(
    pos: Order,
    signal: Optional[TradeSignal],
    *,
    require_signal_weakness: bool,
) -> bool:
    if not require_signal_weakness:
        return True
    if signal is None:
        return False
    return not _signal_supports_position(pos, signal)


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
    max_favorable_r: float,
    no_progress_enabled: bool,
    no_progress_watch_hours: int,
    no_progress_exit_hours: int,
    no_progress_min_mfe_r: float,
    stale_position_review_hours: int,
    signal_supports_position: bool,
    signal_weak_for_position: bool,
) -> str:
    if not time_stop_enabled:
        return "disabled"
    if holding_hours >= stale_position_review_hours:
        if max_favorable_r < no_progress_min_mfe_r and signal_weak_for_position:
            return "stale_position_would_fire"
        return "stale_position_review_keep"
    if not no_progress_enabled:
        return "no_progress_disabled"
    if holding_hours < no_progress_watch_hours:
        return "holding_below_no_progress_watch"
    if max_favorable_r >= no_progress_min_mfe_r:
        return "mfe_progress_ok"
    if holding_hours < no_progress_exit_hours:
        return "no_progress_watch"
    if signal_supports_position:
        return "no_progress_but_signal_supports"
    if signal_weak_for_position:
        return "no_progress_would_fire"
    return "no_progress_waiting_for_signal"


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
    no_progress_enabled: bool = True,
    no_progress_watch_hours: int = 6,
    no_progress_exit_hours: int = 12,
    no_progress_min_mfe_r: float = 0.1,
    no_progress_requires_signal_weakness: bool = True,
    stale_position_review_hours: int = 24,
) -> list[ReviewDecision]:
    """オープンポジションを再評価し、closeまたはrisk compression判断を返す。

    timeout_only=True: halt 中などで signal 不要時に Time Stop のみ評価する。
    no-progress close はデフォルトで signal 弱化を要求するため、signal がない場合は閉じない。
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
        poor_progress = pos.max_favorable_r < no_progress_min_mfe_r
        supports_position = _signal_supports_position(pos, signal)
        weak_for_position = _signal_is_weak_for_position(
            pos,
            signal,
            require_signal_weakness=no_progress_requires_signal_weakness,
        )

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

        if not fired and time_stop_enabled and holding_hours >= stale_position_review_hours:
            if poor_progress and weak_for_position:
                logger.info(
                    f"[REVIEW] {pos.pair}: Time Stop stale position — "
                    f"holding={holding_hours:.1f}h mfe_r={pos.max_favorable_r:.2f} "
                    f"signal_support={supports_position}"
                )
                decisions.append(ReviewDecision(
                    order_id=pos.order_id,
                    pair=pos.pair,
                    close_reason="timeout_stale_position",
                    detail=(
                        f"stale position: held {holding_hours:.1f}h with "
                        f"mfe_r={pos.max_favorable_r:.2f} and weak signal"
                    ),
                ))
                fired = True
            else:
                logger.info(
                    f"[REVIEW] {pos.pair}: stale position review keep — "
                    f"holding={holding_hours:.1f}h mfe_r={pos.max_favorable_r:.2f} "
                    f"signal_support={supports_position}"
                )

        if (
            not fired
            and time_stop_enabled
            and no_progress_enabled
            and poor_progress
            and holding_hours >= no_progress_watch_hours
        ):
            if holding_hours < no_progress_exit_hours:
                logger.info(
                    f"[REVIEW] {pos.pair}: Time Stop no-progress watch — "
                    f"holding={holding_hours:.1f}h mfe_r={pos.max_favorable_r:.2f} "
                    f"< {no_progress_min_mfe_r:.2f}; wait until {no_progress_exit_hours}h"
                )
            elif weak_for_position:
                logger.info(
                    f"[REVIEW] {pos.pair}: Time Stop no progress exit — "
                    f"holding={holding_hours:.1f}h mfe_r={pos.max_favorable_r:.2f} "
                    f"< {no_progress_min_mfe_r:.2f} signal_support={supports_position}"
                )
                decisions.append(ReviewDecision(
                    order_id=pos.order_id,
                    pair=pos.pair,
                    close_reason="timeout_no_progress",
                    detail=(
                        f"Held {holding_hours:.1f}h with mfe_r={pos.max_favorable_r:.2f} "
                        f"< {no_progress_min_mfe_r:.2f} after {no_progress_exit_hours}h; "
                        "signal no longer supports position"
                    ),
                ))
                fired = True
            else:
                logger.info(
                    f"[REVIEW] {pos.pair}: Time Stop no-progress keep — "
                    f"holding={holding_hours:.1f}h mfe_r={pos.max_favorable_r:.2f} "
                    f"signal_support={supports_position}"
                )

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
                max_favorable_r=pos.max_favorable_r,
                no_progress_enabled=no_progress_enabled,
                no_progress_watch_hours=no_progress_watch_hours,
                no_progress_exit_hours=no_progress_exit_hours,
                no_progress_min_mfe_r=no_progress_min_mfe_r,
                stale_position_review_hours=stale_position_review_hours,
                signal_supports_position=supports_position,
                signal_weak_for_position=weak_for_position,
            )
            logger.debug(
                f"[REVIEW] {pos.pair} eval: holding_h={holding_hours:.1f} "
                f"mfe_r={pos.max_favorable_r:.2f} score={score_str} conf={conf_str} "
                f"l1={l1} time_stop={time_stop}"
            )

    return decisions
