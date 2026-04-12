"""signal_only モード用の手動ポジション管理エンドポイント。

エンドポイント:
- POST /manual/open          — ポジション登録
- POST /manual/close/{order_id} — 決済記録
- GET  /manual/list          — 一覧
- POST /manual/balance       — 残高補正
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from src.api._state import state, verify_api_key
from src.trading.position_manager import Order

router = APIRouter(prefix="/manual", tags=["manual"])
logger = logging.getLogger(__name__)


# ── リクエストスキーマ ──────────────────────────────────────────────────────


class OpenRequest(BaseModel):
    pair: str
    direction: str
    entry_price: float
    position_size: float
    stop_loss: float
    take_profit: float
    signal_reason: Optional[str] = ""


class CloseRequest(BaseModel):
    close_price: float
    close_reason: str


class BalanceRequest(BaseModel):
    balance: float


# ── ヘルパー ────────────────────────────────────────────────────────────────


def _get_mgr():
    """manual_position_mgr を取得し、未設定なら 503 を返す。"""
    mgr = state.manual_position_mgr
    if mgr is None:
        raise HTTPException(status_code=503, detail="Manual position manager is not available")
    return mgr


# ── エンドポイント ──────────────────────────────────────────────────────────


@router.post("/open", dependencies=[Depends(verify_api_key)])
def manual_open(req: OpenRequest) -> dict[str, Any]:
    """ポジションを登録する。"""
    mgr = _get_mgr()
    order = Order.new(
        pair=req.pair,
        direction=req.direction,
        entry_price=req.entry_price,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        position_size=req.position_size,
        signal_reason=req.signal_reason or "",
    )
    mgr.open_position(order)
    logger.info(f"[MANUAL] Opened {req.direction.upper()} {req.pair} @ {req.entry_price} (order_id={order.order_id})")
    return {
        "order_id": order.order_id,
        "pair": order.pair,
        "direction": order.direction,
        "entry_price": order.entry_price,
    }


@router.post("/close/{order_id}", dependencies=[Depends(verify_api_key)])
def manual_close(order_id: str, req: CloseRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """決済を記録する。決済後にバックグラウンドで LLM reflection を実行する。"""
    mgr = _get_mgr()

    closed = mgr.close_position(order_id, req.close_price, req.close_reason)
    if closed is None:
        raise HTTPException(status_code=404, detail=f"Order not found: {order_id}")

    account = mgr.get_account_state()

    def _run_reflection() -> None:
        logger.info(f"[MANUAL] Reflection generation would run here for {order_id}")

    background_tasks.add_task(_run_reflection)

    return {
        "order_id": closed.order_id,
        "realized_pnl": round(closed.realized_pnl or 0.0, 2),
        "balance": round(account.balance, 2),
    }


@router.get("/list", dependencies=[Depends(verify_api_key)])
def manual_list() -> dict[str, Any]:
    """オープンポジション一覧と残高を返す。"""
    mgr = _get_mgr()
    account = mgr.get_account_state()
    return {
        "balance": account.balance,
        "open_positions": [p.to_dict() for p in account.open_positions],
    }


@router.post("/balance", dependencies=[Depends(verify_api_key)])
def manual_balance(req: BalanceRequest) -> dict[str, Any]:
    """残高を補正する。"""
    mgr = _get_mgr()
    account = mgr.get_account_state()
    previous = account.balance
    mgr._balance = req.balance
    mgr._save()
    logger.info(f"[MANUAL] Balance adjusted: {previous:.2f} → {req.balance:.2f}")
    return {
        "balance": req.balance,
        "previous": previous,
    }
