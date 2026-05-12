"""ポジション再評価ロジック（Phase 4a）。

Layer 1: シグナル反転決済 — 新シグナルがポジションと逆方向 + 高信頼度 + 最低保有時間達成
Layer 2: タイムアウト決済 — 保有期間超過 + TP方向への進捗不足
Layer 3: 利益ロック決済 — 含み益あり + シグナル減衰
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.signals.signal_combiner import TradeSignal
from src.utils.clock import db_now
from src.trading.position_manager import Order

logger = logging.getLogger(__name__)


@dataclass
class ReviewDecision:
    """ポジション再評価の決済判断。"""
    order_id: str
    pair: str
    close_reason: str  # "reversal" | "timeout" | "profit_lock"
    detail: str


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


def _classify_layer2(
    holding_days: float,
    progress_pct: float,
    max_holding_days: int,
    timeout_min_progress_pct: float,
) -> str:
    if holding_days < max_holding_days:
        return "holding_below_max"
    if progress_pct >= timeout_min_progress_pct:
        return "progress_above_min"
    return "would_fire"


def _classify_layer3(
    signal: Optional[TradeSignal],
    progress_pct: float,
    profit_lock_min_progress_pct: float,
    profit_lock_score_floor: float,
    timeout_only: bool,
) -> str:
    if timeout_only:
        return "skipped_timeout_only"
    if signal is None:
        return "no_signal"
    if progress_pct < profit_lock_min_progress_pct:
        return "below_progress"
    if abs(signal.combined_score) >= profit_lock_score_floor:
        return "signal_still_strong"
    return "would_fire"


def review_open_positions(
    open_positions: list[Order],
    signals_by_pair: dict[str, TradeSignal],
    current_prices: dict[str, float],
    *,
    timeout_only: bool = False,
    reversal_confidence_min: float = 0.70,
    reversal_score_threshold: float = 0.25,
    reversal_min_holding_minutes: int = 240,
    max_holding_days: int = 10,
    timeout_min_progress_pct: float = 0.30,
    profit_lock_min_progress_pct: float = 0.40,
    profit_lock_score_floor: float = 0.15,
) -> list[ReviewDecision]:
    """オープンポジションを再評価し、早期決済すべきものを返す。

    timeout_only=True: halt 中などで signal 不要時に Layer 2 のみ評価する。
                       position_review_enabled gate を bypass する想定 (timeout は安全網)。
    """
    decisions: list[ReviewDecision] = []

    for pos in open_positions:
        signal = signals_by_pair.get(pos.pair)
        price = current_prices.get(pos.pair)
        if price is None:
            continue

        # TP方向への進捗率
        tp_distance = abs(pos.take_profit - pos.entry_price)
        if pos.direction == "buy":
            progress = price - pos.entry_price
        else:
            progress = pos.entry_price - price
        progress_pct = progress / tp_distance if tp_distance > 0 else 0.0

        holding_hours = (db_now() - pos.opened_at).total_seconds() / 3600
        holding_minutes = holding_hours * 60
        holding_days = holding_hours / 24

        fired = False

        # Layer 1: シグナル反転決済 (timeout_only モードでは skip)
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
                logger.info(
                    f"[REVIEW] {pos.pair}: Layer 1 reversal — "
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

        # Layer 2: タイムアウト決済 (timeout_only モードでも評価)
        if not fired and holding_days >= max_holding_days and progress_pct < timeout_min_progress_pct:
            logger.info(
                f"[REVIEW] {pos.pair}: Layer 2 timeout — "
                f"{holding_days:.1f} days, progress {progress_pct:.1%}"
            )
            decisions.append(ReviewDecision(
                order_id=pos.order_id,
                pair=pos.pair,
                close_reason="timeout",
                detail=(
                    f"Held {holding_days:.1f} days with {progress_pct:.1%} "
                    f"progress toward TP (min: {timeout_min_progress_pct:.0%})"
                ),
            ))
            fired = True

        # Layer 3: 利益ロック決済 (timeout_only モードでは skip)
        if (
            not fired
            and not timeout_only
            and signal is not None
            and progress_pct >= profit_lock_min_progress_pct
            and abs(signal.combined_score) < profit_lock_score_floor
        ):
            logger.info(
                f"[REVIEW] {pos.pair}: Layer 3 profit lock — "
                f"progress {progress_pct:.1%}, score={signal.combined_score:+.3f}"
            )
            decisions.append(ReviewDecision(
                order_id=pos.order_id,
                pair=pos.pair,
                close_reason="profit_lock",
                detail=(
                    f"Locking profit at {progress_pct:.1%} progress | "
                    f"signal weakened to {signal.combined_score:+.3f} "
                    f"(floor: +/-{profit_lock_score_floor})"
                ),
            ))
            fired = True

        # 診断ログ (発火しなかった position のみ)
        if not fired:
            score_str = f"{signal.combined_score:+.3f}" if signal else "N/A"
            conf_str = f"{signal.confidence:.2f}" if signal else "N/A"
            l1 = _classify_layer1(
                pos, signal, holding_minutes,
                reversal_min_holding_minutes, reversal_confidence_min,
                reversal_score_threshold, timeout_only,
            )
            l2 = _classify_layer2(
                holding_days, progress_pct,
                max_holding_days, timeout_min_progress_pct,
            )
            l3 = _classify_layer3(
                signal, progress_pct,
                profit_lock_min_progress_pct, profit_lock_score_floor, timeout_only,
            )
            logger.debug(
                f"[REVIEW] {pos.pair} eval: progress={progress_pct:.1%} "
                f"holding_h={holding_hours:.1f} score={score_str} conf={conf_str} "
                f"l1={l1} l2={l2} l3={l3}"
            )

    return decisions
