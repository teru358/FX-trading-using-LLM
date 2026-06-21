"""technical 収集スケジュールの時刻リスト生成 (純関数、テスト容易化のため独立)。"""
from __future__ import annotations

from collections.abc import Callable


def technical_times_for(interval_hours: int) -> list[str]:
    """指定間隔 (時間) の "HH:00" 時刻リストを返す。

    interval_hours=1 → 毎時 (24 個)、2 → 12 個。0/負値は 1 に倒す。
    """
    step = max(1, interval_hours)
    return [f"{h:02d}:00" for h in range(0, 24, step)]


def build_technical_dispatch(
    trade_set: set[str],
    watch_set: set[str],
    run_watch: Callable[[str], None],
    run_trade: Callable[[str], None],
) -> tuple[list[str], Callable[[str], None]]:
    """union 時刻リストと、時刻ごとのディスパッチ関数を返す。

    ディスパッチは単一 slot 内で呼ばれる前提で、時刻が watch_set に入れば watch を、
    trade_set に入れば trade を **watch→trade の順**に同期実行する (slot skip 回避)。
    """
    times = sorted(trade_set | watch_set)

    def dispatch(t: str) -> None:
        if t in watch_set:
            run_watch(t)
        if t in trade_set:
            run_trade(t)

    return times, dispatch
