"""ポジション保護適用の共通 helper (spec §5.1 / review H-b)。

price_monitor と orchestrator の保護 worker が同一の副作用一式 (MFE state 更新 /
pending 合成 / remote-first SL 適用 / pending clear / stage 更新) を共有するため
_apply_profit_protection の中身をここに抽出する。

- execute=False: 判定のみ返す (副作用なし、shadow 比較用)。
- execute=True: 既存 price_monitor._apply_profit_protection と同一の副作用を行う。
  close は実行しない (H4)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.trading.position_protection import (
    compute_mfe_update,
    compute_profit_protection_action,
    more_protective_sl,
)

logger = logging.getLogger(__name__)


@dataclass
class ProtectionApplyResult:
    action: str               # none | raise_sl | close
    stage: str
    target_sl: "float | None"
    mfe_r: float
    giveback_r: float
    updated: bool             # SL を実際に適用したか
    remote_failed: bool


def _apply_sl_target(
    pos,
    target_sl: "float | None",
    stage: str,
    position_mgr,
    broker,
    remote_sync_enabled: bool,
) -> "tuple[bool, bool]":
    """remote-first で SL 適用 (price_monitor._apply_sl_target と同一ロジック)。"""
    if target_sl is None:
        return False, False
    if pos.direction == "buy" and target_sl <= pos.stop_loss:
        return False, False
    if pos.direction == "sell" and target_sl >= pos.stop_loss:
        return False, False
    if remote_sync_enabled and broker is not None:
        if not broker.update_remote_sl(pos.order_id, target_sl):
            return False, True
    updated = position_mgr.update_stop_loss(pos.order_id, target_sl, stage=stage)
    return updated, False


def apply_protection(
    pos,
    *,
    current: float,
    cfg: Any,
    position_mgr,
    broker,
    remote_sync_enabled: bool,
    execute: bool,
) -> ProtectionApplyResult:
    """保護判定を行い、execute=True なら副作用一式を適用する。

    判定 (action/target_sl/mfe_r/giveback_r) は常に返す。execute=False は純粋判定
    (shadow 比較用、副作用ゼロ)。close は実行しない (H4)。
    """
    state = compute_mfe_update(pos, current)
    action = compute_profit_protection_action(pos, current, cfg)

    if not execute:
        return ProtectionApplyResult(
            action=action.action, stage=action.stage, target_sl=action.target_sl,
            mfe_r=state.max_favorable_r, giveback_r=state.giveback_r,
            updated=False, remote_failed=False,
        )

    # --- execute=True: price_monitor._apply_profit_protection と同一の副作用一式 ---
    # (1) MFE state は適用有無に関わらず必ず更新する。
    position_mgr.update_protection_state(
        pos.order_id,
        max_favorable_price=state.max_favorable_price,
        max_favorable_r=state.max_favorable_r,
    )

    # (2) pending と合成して、より保護的な SL target を選ぶ。
    action_target = action.target_sl if action.action == "raise_sl" else None
    pending_target = getattr(pos, "pending_protection_sl", None)
    target_sl = more_protective_sl(pos, action_target, pending_target)
    if target_sl is None:
        return ProtectionApplyResult(
            action=action.action, stage=action.stage, target_sl=action.target_sl,
            mfe_r=state.max_favorable_r, giveback_r=state.giveback_r,
            updated=False, remote_failed=False,
        )

    # (3) remote-first で SL 適用。
    stage = action.stage if target_sl == action_target else "pending"
    updated, remote_failed = _apply_sl_target(
        pos, target_sl, stage, position_mgr, broker, remote_sync_enabled,
    )
    # (4) 適用後: pending clear + last_protection_stage 更新 + ログ。
    if updated:
        if pending_target is not None:
            position_mgr.clear_pending_protection_target(pos.order_id)
        position_mgr.update_protection_state(
            pos.order_id,
            max_favorable_price=state.max_favorable_price,
            max_favorable_r=state.max_favorable_r,
            last_protection_stage=stage,
        )
        logger.info(
            f"[POSITION] {pos.pair} r={state.current_r:+.2f} "
            f"mfe_r={state.max_favorable_r:+.2f} giveback_r={state.giveback_r:.2f} "
            f"stage={stage} sl={target_sl:.5f} remote={'ok' if not remote_failed else 'failed'}"
        )

    return ProtectionApplyResult(
        action=action.action, stage=action.stage, target_sl=action.target_sl,
        mfe_r=state.max_favorable_r, giveback_r=state.giveback_r,
        updated=updated, remote_failed=remote_failed,
    )
