"""Scale-in (順張り増し玉) 判定モジュール。

既存 open ポジに対して、より強い同方向 signal が来た時に追加発注を許可するか判定する。
判定条件 (すべて AND):
  1. 同方向 (BUY-BUY または SELL-SELL) の eligible ポジが 1 つ以上存在
     (eligible = open_confidence/open_score が記録済み = legacy 以外)
  2. signal.confidence > max(eligible.open_confidence) + conf_margin
  3. abs(signal.combined_score) > max(abs(eligible.open_score)) + score_margin
"""
from __future__ import annotations

from dataclasses import dataclass

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order


@dataclass(frozen=True)
class ScaleInDecision:
    """Scale-in 判定結果。"""
    allowed: bool
    reason: str
    prev_max_conf: float | None = None
    prev_max_abs_score: float | None = None


def should_scale_in(
    signal: TradeSignal,
    same_pair_positions: list[Order],
    *,
    conf_margin: float,
    score_margin: float,
) -> ScaleInDecision:
    """既存ポジに対する追加発注 (scale-in) の許可判定。"""
    if signal.action not in ("buy", "sell"):
        return ScaleInDecision(
            allowed=False,
            reason=f"signal action '{signal.action}' is not directional",
        )

    direction = signal.action  # "buy" | "sell"

    eligible = [
        p for p in same_pair_positions
        if p.direction == direction
        and p.open_confidence is not None
        and p.open_score is not None
    ]

    if not eligible:
        return ScaleInDecision(
            allowed=False,
            reason=(
                "no eligible same-direction position with conf/score "
                "(legacy or opposite-direction only)"
            ),
        )

    prev_max_conf = max(p.open_confidence for p in eligible)
    prev_max_abs_score = max(abs(p.open_score) for p in eligible)

    new_conf = signal.confidence
    new_abs_score = abs(signal.combined_score)

    if new_conf <= prev_max_conf + conf_margin:
        return ScaleInDecision(
            allowed=False,
            reason=(
                f"confidence {new_conf:.3f} <= prev_max {prev_max_conf:.3f} "
                f"+ margin {conf_margin:.3f}"
            ),
            prev_max_conf=prev_max_conf,
            prev_max_abs_score=prev_max_abs_score,
        )

    if new_abs_score <= prev_max_abs_score + score_margin:
        return ScaleInDecision(
            allowed=False,
            reason=(
                f"|score| {new_abs_score:.3f} <= prev_max {prev_max_abs_score:.3f} "
                f"+ margin {score_margin:.3f}"
            ),
            prev_max_conf=prev_max_conf,
            prev_max_abs_score=prev_max_abs_score,
        )

    return ScaleInDecision(
        allowed=True,
        reason=(
            f"new conf {new_conf:.3f} > prev_max {prev_max_conf:.3f} "
            f"+ {conf_margin:.3f}, |score| {new_abs_score:.3f} > "
            f"prev_max {prev_max_abs_score:.3f} + {score_margin:.3f}"
        ),
        prev_max_conf=prev_max_conf,
        prev_max_abs_score=prev_max_abs_score,
    )
