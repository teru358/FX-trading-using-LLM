"""ポートフォリオレベルのリスク管理ガード。

ペアごとのポジション数上限と drawdown kill switch を提供する。
paper_trader / mt5_bridge_broker から呼び出す。
"""
from __future__ import annotations

import logging

from src.persistence.balance_snapshot import BalanceSnapshot
from src.trading.position_manager import Order

logger = logging.getLogger(__name__)


def check_max_positions_per_pair(
    pair: str,
    open_positions: list[Order],
    *,
    max_positions_per_pair: int,
) -> str | None:
    """ペアごとのポジション数上限を検証。

    Returns:
        None: 制約 OK
        str: 制約違反の理由メッセージ (発注をスキップすべき)
    """
    same_pair_count = sum(1 for p in open_positions if p.pair == pair)
    if same_pair_count >= max_positions_per_pair:
        return (
            f"{pair} already has {same_pair_count} positions "
            f"(max {max_positions_per_pair})"
        )
    return None


def check_drawdown_kill_switch(
    snap: BalanceSnapshot,
    *,
    enabled: bool,
    max_drawdown_pct: float,
) -> str | None:
    """Drawdown kill switch — peak から max_drawdown_pct 以上落ちたら新規エントリー停止。

    新方式 (Task 5): balance_snapshot.peak_balance を参照。closed_trades 走査・lookback_days は廃止。
    既存ポジションは決済しない。新規エントリーのみブロックする運用保険。
    """
    if not enabled or max_drawdown_pct <= 0:
        return None

    if snap.peak_balance <= 0:
        return f"drawdown kill switch: peak equity non-positive ({snap.peak_balance:.0f})"

    drawdown = (snap.peak_balance - snap.balance) / snap.peak_balance
    if drawdown >= max_drawdown_pct:
        return (
            f"drawdown kill switch: DD {drawdown * 100:.1f}% >= "
            f"{max_drawdown_pct * 100:.1f}% (peak={snap.peak_balance:.0f} "
            f"current={snap.balance:.0f})"
        )
    return None
