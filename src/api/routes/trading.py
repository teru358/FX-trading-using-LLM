"""取引アクション系のエンドポイント。

POST /close/{pair} — ポジションを緊急決済
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.api._state import state, verify_api_key
from src.data.price_fetcher import fetch_current_price
from src.notifications.notifier import OrderClosedEvent, create_notifier
from src.persistence.state_store import StateStore
from src.trading.position_manager import PositionManager

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/close/{pair}", dependencies=[Depends(verify_api_key)])
async def close_position(pair: str) -> dict[str, Any]:
    """ポジションを緊急決済する。"""
    assert state.config is not None

    # PositionManager.close_position は内部で state_store.transaction() を取得し
    # disk から再読込した上で読み書きするため、呼び出し側で明示的にロックを取る
    # 必要はない (read-modify-write の原子性は PositionManager が保証する)。
    state_store = StateStore(state.config.state_dir)
    pm = PositionManager(state_store, context="API_Close")
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

    # live/MT5 経路では実 MT5 ポジ close を broker に依頼。paper は内部 state のみ。
    from src.trading.live_broker import build_close_broker
    broker = build_close_broker(state.config)
    closed = broker.close_position(pos.order_id, current, "manual", pm)

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
