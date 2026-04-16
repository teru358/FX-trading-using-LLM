"""ポートフォリオレベルのリスク管理ガード。

同一通貨グループへの過剰集中と全体エクスポージャーの超過を防ぐ。
paper_trader / live_broker の execute_signal から呼び出す。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.trading.position_manager import Order
from src.utils.clock import db_now

logger = logging.getLogger(__name__)

# 通貨グループ定義: 同じグループに属するペアは相関が高い
_CURRENCY_GROUPS: dict[str, list[str]] = {
    "JPY": ["USDJPY=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X"],
    "USD": ["USDJPY=X", "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "USDCHF=X", "USDCAD=X"],
    "EUR": ["EURUSD=X", "EURJPY=X", "EURGBP=X"],
    "GBP": ["GBPUSD=X", "GBPJPY=X", "EURGBP=X"],
}


def _get_currency_groups(pair: str) -> list[str]:
    """ペアが属する通貨グループ名のリストを返す。"""
    return [group for group, pairs in _CURRENCY_GROUPS.items() if pair in pairs]


def check_portfolio_limits(
    pair: str,
    direction: str,
    position_size: float,
    open_positions: list[Order],
    *,
    max_positions_per_group: int = 2,
    max_total_positions: int = 4,
    max_same_direction_per_group: int = 2,
) -> str | None:
    """ポートフォリオ制約を検証する。

    Returns:
        None: 制約OK、発注可能
        str: 制約違反の理由メッセージ（発注をスキップすべき）
    """
    # 1. 全体ポジション数の上限
    if len(open_positions) >= max_total_positions:
        return (
            f"Max total positions reached ({len(open_positions)}/{max_total_positions})"
        )

    # 2. 通貨グループごとの制限
    groups = _get_currency_groups(pair)
    for group in groups:
        group_pairs = set(_CURRENCY_GROUPS[group])
        group_positions = [p for p in open_positions if p.pair in group_pairs]

        # グループ内ポジション数
        if len(group_positions) >= max_positions_per_group:
            return (
                f"{group} group already has {len(group_positions)} positions "
                f"(max {max_positions_per_group})"
            )

        # グループ内の同方向ポジション数
        same_dir_count = sum(1 for p in group_positions if p.direction == direction)
        if same_dir_count >= max_same_direction_per_group:
            return (
                f"{group} group already has {same_dir_count} {direction} positions "
                f"(max {max_same_direction_per_group})"
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

    Args:
        initial_balance: 元本 (peak の初期値として使う)
        closed_trades: これまでのクローズ済みトレード一覧
        enabled: False ならスキップ (None 返却)
        max_drawdown_pct: peak からの下落率しきい値 (0.10 = 10%)
        lookback_days: 0 なら全期間、>0 なら直近 N 日のクローズトレードのみ参照
        now: 時刻 (テスト用、未指定なら db_now())

    Returns:
        None: OK、新規エントリー可
        str: 制約違反理由メッセージ
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

    # closed_at 昇順でエクイティカーブを構築 (None は末尾へ)
    trades.sort(key=lambda t: t.closed_at or datetime.max)

    running = initial_balance
    peak = initial_balance
    for t in trades:
        running += t.realized_pnl or 0
        if running > peak:
            peak = running

    if peak <= 0:
        # 元本毀損で peak が 0 以下 → 極端ケース、即停止
        return f"drawdown kill switch: peak equity non-positive ({peak:.0f})"

    current = running  # 全トレード反映後 = 現在の実現残高
    drawdown = (peak - current) / peak
    if drawdown >= max_drawdown_pct:
        return (
            f"drawdown kill switch: DD {drawdown * 100:.1f}% >= "
            f"{max_drawdown_pct * 100:.1f}% (peak={peak:.0f} current={current:.0f}"
            + (f", lookback={lookback_days}d" if lookback_days else "")
            + ")"
        )
    return None
