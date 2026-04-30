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
from typing import TYPE_CHECKING, Literal

from src.signals.signal_combiner import TradeSignal
from src.trading.position_manager import Order

if TYPE_CHECKING:
    from src.trading.position_manager import PositionManager


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


PreExecStatus = Literal["allowed", "scale_in", "skip"]


def evaluate_pre_execution_checks(
    signal: TradeSignal,
    position_mgr: "PositionManager",
    *,
    max_positions_per_pair: int,
    scale_in_enabled: bool,
    scale_in_conf_margin: float,
    scale_in_score_margin: float,
    drawdown_kill_switch_enabled: bool,
    drawdown_kill_switch_max_pct: float,
    drawdown_kill_switch_lookback_days: int,
) -> tuple[PreExecStatus, str]:
    """発注前の共通チェックを実施。

    Returns:
        ("allowed", reason): 通常発注可
        ("scale_in", reason): scale-in 発注可 (reason に判定詳細)
        ("skip", reason): 発注スキップ (reason に理由)
    """
    from src.trading.portfolio_guard import (
        check_drawdown_kill_switch,
        check_max_positions_per_pair,
    )

    # 1. ペアごと上限チェック
    same_pair_positions = position_mgr.get_open_positions_by_pair(signal.pair)
    account = position_mgr.get_account_state()
    pair_limit_rejection = check_max_positions_per_pair(
        signal.pair,
        account.open_positions,
        max_positions_per_pair=max_positions_per_pair,
    )
    if pair_limit_rejection:
        return "skip", pair_limit_rejection

    # 2. 既存ポジあり → scale-in 判定
    decision_reason = "no existing position, normal entry"
    is_scale_in = False
    if same_pair_positions:
        if not scale_in_enabled:
            return "skip", f"{signal.pair} already has open position (scale-in disabled)"
        decision = should_scale_in(
            signal,
            same_pair_positions,
            conf_margin=scale_in_conf_margin,
            score_margin=scale_in_score_margin,
        )
        if not decision.allowed:
            return "skip", f"scale-in rejected: {decision.reason}"
        is_scale_in = True
        decision_reason = decision.reason

    # 3. DD kill switch
    dd_rejection = check_drawdown_kill_switch(
        initial_balance=account.initial_balance,
        closed_trades=account.closed_trades,
        enabled=drawdown_kill_switch_enabled,
        max_drawdown_pct=drawdown_kill_switch_max_pct,
        lookback_days=drawdown_kill_switch_lookback_days,
    )
    if dd_rejection:
        return "skip", dd_rejection

    # 4. 結果決定
    if is_scale_in:
        return "scale_in", decision_reason
    return "allowed", decision_reason
