"""ポジション再評価ロジック（Phase 4a）。

Layer 1: シグナル反転決済 — 新シグナルがポジションと逆方向 + 高信頼度
Layer 2: タイムアウト決済 — 保有期間超過 + TP方向への進捗不足
Layer 3: 利益ロック決済 — 含み益あり + シグナル減衰
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


def review_open_positions(
    open_positions: list[Order],
    signals_by_pair: dict[str, TradeSignal],
    current_prices: dict[str, float],
    *,
    reversal_confidence_min: float = 0.70,
    reversal_score_threshold: float = 0.25,
    max_holding_days: int = 10,
    timeout_min_progress_pct: float = 0.30,
    profit_lock_min_progress_pct: float = 0.40,
    profit_lock_score_floor: float = 0.15,
) -> list[ReviewDecision]:
    """オープンポジションを再評価し、早期決済すべきものを返す。"""
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

        # Layer 1: シグナル反転決済
        if signal is not None:
            is_reversal = (
                (pos.direction == "buy" and signal.predicted_direction == "bearish")
                or (pos.direction == "sell" and signal.predicted_direction == "bullish")
            )
            if (
                is_reversal
                and signal.confidence >= reversal_confidence_min
                and abs(signal.combined_score) >= reversal_score_threshold
            ):
                logger.info(
                    f"[REVIEW] {pos.pair}: Layer 1 reversal — "
                    f"{pos.direction} vs {signal.predicted_direction} "
                    f"(score={signal.combined_score:+.3f} conf={signal.confidence:.2f})"
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
                continue

        # Layer 2: タイムアウト決済
        holding_hours = (db_now() - pos.opened_at).total_seconds() / 3600
        holding_days = holding_hours / 24
        if holding_days >= max_holding_days and progress_pct < timeout_min_progress_pct:
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
            continue

        # Layer 3: 利益ロック決済
        if (
            signal is not None
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
            continue

    return decisions
