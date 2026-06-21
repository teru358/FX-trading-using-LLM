"""technical 収集スケジュールの時刻リスト生成 (純関数、テスト容易化のため独立)。"""
from __future__ import annotations


def technical_times_for(interval_hours: int) -> list[str]:
    """指定間隔 (時間) の "HH:00" 時刻リストを返す。

    interval_hours=1 → 毎時 (24 個)、2 → 12 個。0/負値は 1 に倒す。
    """
    step = max(1, interval_hours)
    return [f"{h:02d}:00" for h in range(0, 24, step)]
