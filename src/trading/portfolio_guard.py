"""ポートフォリオレベルのリスク管理ガード。

ペアごとのポジション数上限と drawdown kill switch を提供する。
paper_trader / mt5_bridge_broker から呼び出す。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.trading.position_manager import Order
from src.utils.clock import db_now

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
    initial_balance: float,
    closed_trades: list[Order],
    *,
    enabled: bool,
    max_drawdown_pct: float,
    lookback_days: int = 0,
    now: datetime | None = None,
) -> str | None:
    """Drawdown kill switch — peak equity から max_drawdown_pct 以上落ちたら新規エントリー停止。

    既存ポジションは決済しない。新規エントリーのみブロックする運用保険。
    """
    if not enabled or max_drawdown_pct <= 0:
        return None

    now_ts = now or db_now()
    if lookback_days and lookback_days > 0:
        cutoff = now_ts - timedelta(days=lookback_days)
        trades = [
            t for t in closed_trades
            if t.closed_at is not None and t.closed_at >= cutoff
        ]
    else:
        trades = list(closed_trades)

    trades.sort(key=lambda t: t.closed_at or datetime.max)

    running = initial_balance
    peak = initial_balance
    for t in trades:
        running += t.realized_pnl or 0
        if running > peak:
            peak = running

    if peak <= 0:
        return f"drawdown kill switch: peak equity non-positive ({peak:.0f})"

    current = running
    drawdown = (peak - current) / peak
    if drawdown >= max_drawdown_pct:
        return (
            f"drawdown kill switch: DD {drawdown * 100:.1f}% >= "
            f"{max_drawdown_pct * 100:.1f}% (peak={peak:.0f} current={current:.0f}"
            + (f", lookback={lookback_days}d" if lookback_days else "")
            + ")"
        )
    return None
