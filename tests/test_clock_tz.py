"""DB 時刻規約 (naive machine-local) への変換ヘルパのテスト。

local_tz を注入して実行環境 TZ に依存せず検証する (UTC CI でも偽陽性にしない)。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.utils.clock import to_db_naive_datetime, to_db_naive_index

JST = ZoneInfo("Asia/Tokyo")


def test_to_db_naive_datetime_aware_utc_to_local():
    """tz-aware UTC は注入 local_tz でローカル変換され naive 化される。"""
    aware = datetime(2026, 6, 29, 10, 0, 0, tzinfo=timezone.utc)
    out = to_db_naive_datetime(aware, local_tz=JST)
    assert out.tzinfo is None
    assert out == datetime(2026, 6, 29, 19, 0, 0)  # 10:00 UTC = 19:00 JST


def test_to_db_naive_datetime_naive_passthrough():
    """既に naive はそのまま (二重変換しない)。"""
    naive = datetime(2026, 6, 29, 10, 0, 0)
    assert to_db_naive_datetime(naive, local_tz=JST) == naive


def test_to_db_naive_index_aware_utc_to_local():
    """tz-aware UTC の DatetimeIndex を JST naive に変換する。"""
    idx = pd.DatetimeIndex(["2026-06-29 10:00:00", "2026-06-29 11:00:00"], tz="UTC")
    out = to_db_naive_index(idx, local_tz=JST)
    assert out.tz is None
    assert list(out) == [pd.Timestamp("2026-06-29 19:00:00"),
                         pd.Timestamp("2026-06-29 20:00:00")]


def test_to_db_naive_index_naive_passthrough():
    """既に naive な index はそのまま。"""
    idx = pd.DatetimeIndex(["2026-06-29 10:00:00"])
    out = to_db_naive_index(idx, local_tz=JST)
    assert out.tz is None
    assert out[-1] == pd.Timestamp("2026-06-29 10:00:00")
