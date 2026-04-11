"""FX市場の開閉判定。

FX市場はニューヨーク時間(Eastern Time)を基準に開閉する。
  開場: 日曜 17:00 ET
  閉場: 金曜 17:00 ET

ZoneInfo("America/New_York") を使用するため、
EST(UTC-5) ↔ EDT(UTC-4) のサマータイム切替は自動的に考慮される。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def is_market_open(now: datetime | None = None) -> bool:
    """FX市場が現在開いているかを返す。

    Args:
        now: 判定基準日時（None の場合は現在時刻）。タイムゾーン付きでも naive でも可。
    """
    if now is None:
        et = datetime.now(_ET)
    else:
        et = now.astimezone(_ET) if now.tzinfo is not None else now.replace(tzinfo=_ET)

    wd = et.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    h = et.hour

    if wd == 4 and h >= 17:   # 金曜 17:00 ET 以降 → 閉場
        return False
    if wd == 5:               # 土曜は終日閉場
        return False
    if wd == 6 and h < 17:   # 日曜 17:00 ET 前 → 閉場
        return False

    return True


def market_status_label(now: datetime | None = None) -> str:
    """ログ用のステータス文字列を返す（例: 'closed (Saturday ET)'）。"""
    if now is None:
        et = datetime.now(_ET)
    else:
        et = now.astimezone(_ET) if now.tzinfo is not None else now.replace(tzinfo=_ET)

    day = et.strftime("%A")
    return "open" if is_market_open(now) else f"closed ({day} {et.strftime('%H:%M')} ET)"
