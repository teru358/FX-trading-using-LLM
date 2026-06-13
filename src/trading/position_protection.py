"""Position protection calculations based on R, MFE, and giveback.

This module is intentionally side-effect free. Persistence and broker calls belong
in position_manager / price_monitor.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.trading.position_manager import Order


@dataclass(frozen=True)
class ProtectionStateUpdate:
    max_favorable_price: float | None
    max_favorable_r: float
    current_r: float
    giveback_r: float


@dataclass(frozen=True)
class ProtectionAction:
    action: str  # "none" | "raise_sl" | "close"
    target_sl: float | None
    stage: str
    reason: str


def risk_distance(pos: Order) -> float:
    """Return the initial risk distance in price units."""
    if pos.initial_risk_price_distance > 0:
        return float(pos.initial_risk_price_distance)
    initial_sl = pos.initial_stop_loss if pos.initial_stop_loss != 0.0 else pos.stop_loss
    return abs(float(pos.entry_price) - float(initial_sl))


def current_r(pos: Order, current: float) -> float:
    """Return current unrealized move in R units."""
    risk = risk_distance(pos)
    if risk <= 0:
        return 0.0
    if pos.direction == "buy":
        return (current - pos.entry_price) / risk
    return (pos.entry_price - current) / risk


def compute_mfe_update(pos: Order, current: float) -> ProtectionStateUpdate:
    """Compute current R, max favorable R, and giveback R without mutating state."""
    cur_r = current_r(pos, current)
    current_favorable_price: float | None = current if cur_r > 0 else None

    max_price = pos.max_favorable_price
    max_r = max(float(pos.max_favorable_r), 0.0)

    if current_favorable_price is not None and cur_r > max_r:
        max_price = current_favorable_price
        max_r = cur_r

    giveback = max(max_r - cur_r, 0.0)
    return ProtectionStateUpdate(
        max_favorable_price=max_price,
        max_favorable_r=max_r,
        current_r=cur_r,
        giveback_r=giveback,
    )


def more_protective_sl(pos: Order, left: float | None, right: float | None) -> float | None:
    """Pick the SL that protects more profit for the position direction."""
    if left is None:
        return right
    if right is None:
        return left
    if pos.direction == "buy":
        return max(left, right)
    return min(left, right)


def _is_worse_or_equal_than_current_sl(pos: Order, target_sl: float) -> bool:
    if pos.direction == "buy":
        return target_sl <= pos.stop_loss
    return target_sl >= pos.stop_loss


def _round_price(value: float) -> float:
    return round(value, 5)


def _stage_target_sl(pos: Order, stage: str) -> float:
    risk = risk_distance(pos)
    initial_sl = pos.initial_stop_loss if pos.initial_stop_loss != 0.0 else pos.stop_loss
    if stage == "half":
        return _round_price((pos.entry_price + initial_sl) / 2.0)
    if stage == "breakeven":
        return _round_price(pos.entry_price)
    if stage == "lock":
        locked_r = 0.3
        if pos.direction == "buy":
            return _round_price(pos.entry_price + risk * locked_r)
        return _round_price(pos.entry_price - risk * locked_r)
    raise ValueError(f"unknown protection stage: {stage}")


def compute_profit_protection_action(pos: Order, current: float, cfg: Any) -> ProtectionAction:
    """Return the profit protection action for a position at current price."""
    risk = risk_distance(pos)
    if risk <= 0:
        return ProtectionAction("none", None, "none", "invalid initial risk")

    update = compute_mfe_update(pos, current)
    if (
        update.max_favorable_r >= float(cfg.giveback_close_min_mfe_r)
        and update.giveback_r >= float(cfg.giveback_close_r)
    ):
        return ProtectionAction(
            "close",
            None,
            "giveback",
            f"giveback {update.giveback_r:.2f}R from MFE {update.max_favorable_r:.2f}R",
        )

    stage = "none"
    if update.max_favorable_r >= float(cfg.protect_lock_r):
        stage = "lock"
    elif update.max_favorable_r >= float(cfg.protect_breakeven_r):
        stage = "breakeven"
    elif update.max_favorable_r >= float(cfg.protect_half_r):
        stage = "half"

    if stage == "none":
        return ProtectionAction("none", None, "none", "below protection threshold")

    target = _stage_target_sl(pos, stage)
    if _is_worse_or_equal_than_current_sl(pos, target):
        return ProtectionAction("none", None, "none", "target does not improve current SL")

    return ProtectionAction(
        "raise_sl",
        target,
        stage,
        f"protect {stage} at MFE {update.max_favorable_r:.2f}R",
    )
