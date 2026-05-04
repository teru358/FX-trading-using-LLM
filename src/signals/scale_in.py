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
from src.trading.portfolio_guard import (
    check_drawdown_kill_switch,
    check_max_positions_per_pair,
)
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


@dataclass(frozen=True)
class PreExecResult:
    """Pre-execution check result. Enables uniform broker logging."""
    status: PreExecStatus  # "allowed" | "scale_in" | "skip"
    reason: str
    decision: "ScaleInDecision | None" = None  # populated when status == "scale_in"


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
) -> PreExecResult:
    """発注前の共通チェックを実施。"""
    # 1. ペアごと上限チェック
    same_pair_positions = position_mgr.get_open_positions_by_pair(signal.pair)
    account = position_mgr.get_account_state()
    pair_limit_rejection = check_max_positions_per_pair(
        signal.pair,
        account.open_positions,
        max_positions_per_pair=max_positions_per_pair,
    )
    if pair_limit_rejection:
        return PreExecResult(status="skip", reason=pair_limit_rejection)

    # 2. 既存ポジあり → scale-in 判定
    decision: ScaleInDecision | None = None
    is_scale_in = False
    decision_reason = "no existing position, normal entry"
    if same_pair_positions:
        if not scale_in_enabled:
            return PreExecResult(
                status="skip",
                reason=f"{signal.pair} already has open position (scale-in disabled)",
            )
        decision = should_scale_in(
            signal,
            same_pair_positions,
            conf_margin=scale_in_conf_margin,
            score_margin=scale_in_score_margin,
        )
        if not decision.allowed:
            return PreExecResult(
                status="skip",
                reason=f"scale-in rejected: {decision.reason}",
                decision=decision,
            )
        is_scale_in = True
        decision_reason = decision.reason

    # 3. DD kill switch — account.peak_balance / account.balance から判定
    # DD 無効時は早期 return で skip。
    # 注: テストの MagicMock(spec=PositionManager) は get_account_state を持つが
    # 内部 _store/_peak_balance は持たないため、AccountState 経由の値を使う。
    # check_drawdown_kill_switch は BalanceSnapshot を要求するので、DD 判定に
    # 必要な balance/peak だけ詰めた placeholder snapshot を構築する
    # (source/fetched_at は DD ロジック内で参照されないため安全)。
    if drawdown_kill_switch_enabled and drawdown_kill_switch_max_pct > 0:
        from src.persistence.balance_snapshot import BalanceSnapshot
        _snap = BalanceSnapshot(
            balance=account.balance,
            deposit=account.initial_balance,
            peak_balance=account.peak_balance,
            source="paper",  # placeholder — DD logic only reads balance/peak
            fetched_at="",  # placeholder — is_stale() not called here
        )
        dd_rejection = check_drawdown_kill_switch(
            _snap,
            enabled=drawdown_kill_switch_enabled,
            max_drawdown_pct=drawdown_kill_switch_max_pct,
        )
        if dd_rejection:
            return PreExecResult(status="skip", reason=dd_rejection, decision=decision)

    # 4. 結果決定
    if is_scale_in:
        return PreExecResult(status="scale_in", reason=decision_reason, decision=decision)
    return PreExecResult(status="allowed", reason=decision_reason)
