"""残高 + ポジション情報を返す /account endpoint。

旧 /status の内容を分離。/status は Phase 3b でシステム健全性レポートに変更済み。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from src.api._state import state, verify_api_key
from src.data.price_fetcher import fetch_current_price
from src.persistence.state_store import StateStore
from src.trading.position_manager import PositionManager

router = APIRouter()


@router.get("/account", dependencies=[Depends(verify_api_key)])
def account() -> dict[str, Any]:
    """残高 + 損益 + 取引数 + 勝率 + オープンポジション。"""
    assert state.config is not None
    state_store = StateStore(state.config.state_dir)
    pm = PositionManager(state_store, context="API_Account")
    acc = pm.get_account_state()

    positions = []
    for pos in acc.open_positions:
        entry: dict[str, Any] = {
            "pair": pos.pair,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "position_size": pos.position_size,
            "opened_at": pos.opened_at.isoformat(),
        }
        try:
            current = fetch_current_price(pos.pair).price
            mult = 1 if pos.direction == "buy" else -1
            entry["current_price"] = current
            entry["unrealized_pnl"] = round(
                (current - pos.entry_price) * pos.position_size * mult, 2
            )
        except Exception:
            entry["current_price"] = None
            entry["unrealized_pnl"] = None
        positions.append(entry)

    pnl = acc.balance - acc.initial_balance
    return {
        "balance": acc.balance,
        "initial_balance": acc.initial_balance,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / acc.initial_balance * 100, 2),
        "total_trades": acc.total_trades,
        "win_rate": round(acc.win_rate() * 100, 1),
        "open_positions": positions,
    }
