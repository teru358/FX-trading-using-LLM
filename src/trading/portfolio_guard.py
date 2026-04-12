"""ポートフォリオレベルのリスク管理ガード。

同一通貨グルー���への過剰集中と全体エクスポージャーの超過を防ぐ。
paper_trader / live_broker の execute_signal から呼び出す。
"""
from __future__ import annotations

import logging

from src.trading.position_manager import Order

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
