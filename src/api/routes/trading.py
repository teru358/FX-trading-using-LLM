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


# 通知 OFF 時の blocking 待機の最大時間 = run_trade_soft_timeout_sec × この倍率
# 取引サイクルは LLM ニュース分析 + テクニカル + ペア並列分析で 1〜3 分かかる想定。
# 他ジョブを待つ場合も含め、デフォルト 10s × 60 = 600s (10 分) を上限とする。
_BLOCKING_WAIT_MULTIPLIER = 60


def _build_run_trade_response(started_at, status: str = "completed") -> dict[str, Any]:
    """run_trading_cycle 完了後に返すレスポンス body を組み立てる。"""
    assert state.config is not None
    elapsed = (db_now() - started_at).total_seconds()
    state_store = StateStore(state.config.state_dir)
    pm = PositionManager(
        state_store, state.config.trading.initial_balance, context="API_RunTrade",
    )
    account = pm.get_account_state()
    return {
        "status": status,
        "elapsed_seconds": round(elapsed, 1),
        "executed_at": started_at.isoformat(),
        "balance": account.balance,
        "open_positions_count": len(account.open_positions),
        "total_trades": account.total_trades,
    }


@router.post("/run/trade", dependencies=[Depends(verify_api_key)])
def run_trade() -> dict[str, Any]:
    """取引判定ループを手動実行する。

    挙動は Discord 通知設定によって分岐する:

    - **通知 ON** (notifier.enabled=true): ``soft_timeout`` 内完了で同期返答、
      超過で "accepted" 返答 + バックグラウンド継続 + 完了時 Discord 通知。
    - **通知 OFF**: 走行中ジョブがあれば完了を待ってから順次実行 (最大
      ``run_trade_soft_timeout_sec * 60`` 秒 = デフォルト 600s)。HTTP リクエストは
      その間ブロックする。
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

    # ── 通知 OFF: 同期 blocking で順次実行 ─────────────────────
    if not state.config.notifier.enabled:
        max_wait = soft_timeout * _BLOCKING_WAIT_MULTIPLIER
        job_status, result = state.llm_slot.run_user_blocking_with_timeout(
            _job, timeout=max_wait,
        )
        if job_status == "completed":
            return _build_run_trade_response(started_at)
        if job_status == "completed_with_error":
            raise HTTPException(status_code=500, detail=f"run_trade failed: {result}")
        # timeout
        raise HTTPException(
            status_code=503,
            detail=(
                f"他のジョブが走行中で {max_wait:.0f}s 以内にスロットを取得できませんでした。"
                f"後ほど再試行してください。"
            ),
        )

    # ── 通知 ON: 従来通り soft_timeout 内同期、超過で webhook ──
    job_status, result = state.llm_slot.try_run_user_sync(_job, soft_timeout=soft_timeout)

    if job_status == "completed":
        return _build_run_trade_response(started_at)

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

    # PositionManager.close_position は内部で state_store.transaction() を取得し
    # disk から再読込した上で読み書きするため、呼び出し側で明示的にロックを取る
    # 必要はない (read-modify-write の原子性は PositionManager が保証する)。
    state_store = StateStore(state.config.state_dir)
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

    # TradingView チャート再描画 (ポジション線をクリア)
    if state.config.tradingview.enabled:
        try:
            from src.tradingview.tv_payload import render_tv_chart
            await render_tv_chart(state.config, state.analysis_store, pm, reason="API close", force=True)
        except Exception as e:
            logger.warning(f"[API] TV chart update failed: {e}")

    return {
        "closed":       True,
        "pair":         closed.pair,
        "direction":    closed.direction,
        "entry_price":  closed.entry_price,
        "close_price":  current,
        "realized_pnl": round(closed.realized_pnl or 0.0, 2),
        "balance":      round(pm.get_account_state().balance, 2),
    }
