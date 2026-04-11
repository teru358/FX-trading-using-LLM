"""取引アクション系のエンドポイント。

POST /run/trade   — 取引判定ループを手動実行 (soft_timeout 内なら同期、超過で webhook)
POST /close/{pair} — ポジションを緊急決済
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.api._state import state, verify_api_key
from src.api.notifications import handle_promoted_or_slot_busy, notify_trade_complete
from src.data.price_fetcher import fetch_current_price
from src.notifications.notifier import OrderClosedEvent, create_notifier
from src.persistence.state_store import StateStore
from src.trading.position_manager import PositionManager
from src.utils.clock import db_now

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/run/trade", dependencies=[Depends(verify_api_key)])
def run_trade() -> dict[str, Any]:
    """取引判定ループを手動実行する。

    soft_timeout 内に完了したら結果を同期返答。超過したら "accepted" を返し、
    バックグラウンドで継続 + 完了時に Discord webhook で通知する。
    既に他の LLM ジョブが走行中なら即座に "accepted" を返してキュー待機する。
    """
    assert state.config is not None and state.llm_slot is not None
    assert state.store is not None and state.analysis_store is not None
    assert state.price_store is not None and state.hold_store is not None

    from src.cycles.trading import run_trading_cycle

    started_at = db_now()
    soft_timeout = state.config.api.run_trade_soft_timeout_sec

    def _job() -> None:
        run_trading_cycle(
            state.config, state.store, state.price_store, state.analysis_store, state.hold_store,
        )

    job_status, result = state.llm_slot.try_run_user_sync(_job, soft_timeout=soft_timeout)

    if job_status == "completed":
        # 同期で完了 → 現在の口座状態を返す
        elapsed = (db_now() - started_at).total_seconds()
        state_store = StateStore(state.config.state_dir)
        pm = PositionManager(state_store, state.config.trading.initial_balance, context="API_RunTrade")
        account = pm.get_account_state()
        return {
            "status": "completed",
            "elapsed_seconds": round(elapsed, 1),
            "executed_at": started_at.isoformat(),
            "balance": account.balance,
            "open_positions_count": len(account.open_positions),
            "total_trades": account.total_trades,
        }

    if job_status == "completed_with_error":
        raise HTTPException(status_code=500, detail=f"run_trade failed: {result}")

    # promoted or slot_busy → 完了 webhook / background queue
    handle_promoted_or_slot_busy(
        status=job_status,
        job=_job,
        slot=state.llm_slot,
        started_at=started_at,
        job_name="取引サイクル",
        on_complete_when_busy=lambda r, e: notify_trade_complete(started_at, e),
    )

    return {
        "status": "accepted",
        "message": "取引サイクルを実行中です。完了時に Discord へ通知します。",
        "started_at": started_at.isoformat(),
    }


@router.post("/close/{pair}", dependencies=[Depends(verify_api_key)])
async def close_position(pair: str) -> dict[str, Any]:
    """ポジションを緊急決済する。"""
    assert state.config is not None

    # state_store のファイルロックを取得してから PositionManager を作成し、
    # load → close → save の read-modify-write を他のジョブと直列化する。
    # これにより price_monitor のトレーリング更新や run_trading_cycle の
    # 決済処理との競合を防ぐ。
    state_store = StateStore(state.config.state_dir)
    with state_store._lock:
        pm = PositionManager(state_store, state.config.trading.initial_balance, context="API_Close")
        account = pm.get_account_state()

        pos = next(
            (p for p in account.open_positions if p.pair.upper() == pair.upper()),
            None,
        )
        if pos is None:
            open_pairs = [p.pair for p in account.open_positions]
            raise HTTPException(
                status_code=404,
                detail=f"Position not found: {pair}. Open: {open_pairs}",
            )

        try:
            current = fetch_current_price(pos.pair).price
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Price fetch failed: {e}")

        closed = pm.close_position(pos.order_id, current, "manual")

    if closed is None:
        raise HTTPException(status_code=500, detail="Close failed")

    # 非同期通知 (ベストエフォート)
    if state.config.notifier.notify_on_order_close:
        try:
            notifier = create_notifier(state.config.notifier.enabled)
            await notifier.notify_order_closed(OrderClosedEvent(
                pair=closed.pair,
                direction=closed.direction,
                entry_price=closed.entry_price,
                close_price=current,
                realized_pnl=closed.realized_pnl or 0.0,
                close_reason="manual",
                balance=pm.get_account_state().balance,
                source="manual",
            ))
        except Exception as e:
            logger.warning(f"[API] Close notification failed: {e}")

    return {
        "closed":       True,
        "pair":         closed.pair,
        "direction":    closed.direction,
        "entry_price":  closed.entry_price,
        "close_price":  current,
        "realized_pnl": round(closed.realized_pnl or 0.0, 2),
        "balance":      round(pm.get_account_state().balance, 2),
    }
