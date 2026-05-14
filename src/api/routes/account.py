"""残高 + ポジション情報 + MT5 実残高 + 乖離を返す /account endpoint。

internal (台帳) + mt5 (実残高) + divergence の 3 セクション構成:
- internal: PositionManager + balance.json 由来 (常に値あり)
- mt5: live/live_test モードかつ bridge `/account` 取得成功時のみ非 null
- divergence: 上記 2 つが揃ったときのみ非 null

halt 状態は本 endpoint からは返さない。`/status` の `halt` セクションで確認する
(/status 統合に伴い /account からは除去)。
"""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends

from src.api._state import state, verify_api_key
from src.data.price_fetcher import fetch_current_price
from src.persistence import balance_snapshot
from src.persistence.state_store import StateStore
from src.trading.position_manager import PositionManager

router = APIRouter()


@router.get("/account", dependencies=[Depends(verify_api_key)])
def account() -> dict[str, Any]:
    """残高 + 損益 + 取引数 + 勝率 + オープンポジション + MT5 実残高 (live のみ)。"""
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

    snap = balance_snapshot.read(state.config.state_dir)
    pnl = snap.balance - snap.deposit
    pnl_pct = (pnl / snap.deposit * 100) if snap.deposit > 0 else 0.0
    drawdown_pct = 0.0
    if snap.peak_balance > 0:
        drawdown_pct = (snap.peak_balance - snap.balance) / snap.peak_balance * 100

    internal = {
        "balance":         snap.balance,
        "deposit":         snap.deposit,
        "peak_balance":    snap.peak_balance,
        "drawdown_pct":    round(drawdown_pct, 2),
        "pnl":             round(pnl, 2),
        "pnl_pct":         round(pnl_pct, 2),
        "total_trades":    acc.total_trades,
        "win_rate":        round(acc.win_rate() * 100, 1),
        "open_positions": positions,
    }

    mt5_data: dict[str, Any] | None = None
    divergence: dict[str, Any] | None = None
    if state.config.mode in ("live", "live_test") and state.config.providers.mt5:
        mt5_cfg = state.config.providers.mt5
        if mt5_cfg.bridge_url:
            try:
                headers = (
                    {"X-Bridge-Api-Key": mt5_cfg.api_key} if mt5_cfg.api_key else {}
                )
                resp = httpx.get(
                    f"{mt5_cfg.bridge_url.rstrip('/')}/account",
                    timeout=2.0, headers=headers,
                )
                resp.raise_for_status()
                d = resp.json()
                mt5_data = {
                    "balance":     d.get("balance"),
                    "equity":      d.get("equity"),
                    "free_margin": d.get("free_margin"),
                    "margin":      d.get("margin"),
                    # snapshot_fetched_at: balance.json の最終更新時刻 (heartbeat 由来)。
                    # この /account 呼出のタイムスタンプではない点に注意 (誤読防止のため命名変更)。
                    "snapshot_fetched_at": snap.fetched_at,
                }
                if mt5_data["balance"] is not None:
                    diff = mt5_data["balance"] - snap.balance
                    # 内部 balance が 0 のときは divergence_pct を None (誤った 0% 表示を防ぐ)
                    diff_pct = (
                        round(diff / snap.balance * 100, 2)
                        if snap.balance > 0 else None
                    )
                    divergence = {
                        "balance_diff":     round(diff, 2),
                        "balance_diff_pct": diff_pct,
                    }
            except (httpx.HTTPError, ValueError):
                pass  # 失敗時は mt5/divergence を null のまま

    return {
        "internal":   internal,
        "mt5":        mt5_data,
        "divergence": divergence,
    }
