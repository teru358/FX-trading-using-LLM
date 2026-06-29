"""econ 影響分析: event_time が naive local に変換され load_ohlcv の窓に使われる配線テスト。"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import src.utils.clock as clock
from src.jobs.technical_collector import _econ_window_for

_JST = ZoneInfo("Asia/Tokyo")


def test_econ_window_uses_local_naive(monkeypatch):
    """aware UTC event_time から ±1h の local naive 窓 (start, end) を返す。"""
    monkeypatch.setattr(clock, "_resolve_local_tz", lambda tz=None: _JST)
    ev_time = datetime(2026, 6, 29, 10, 0, 0, tzinfo=timezone.utc)  # 19:00 JST

    start, end = _econ_window_for(ev_time)

    assert start.tzinfo is None and end.tzinfo is None
    assert start == datetime(2026, 6, 29, 18, 0, 0)   # 19:00 JST - 1h
    assert end == datetime(2026, 6, 29, 20, 0, 0)     # 19:00 JST + 1h


def test_econ_window_naive_passthrough(monkeypatch):
    """既に naive な event_time はそのまま基準に ±1h 窓を返す (二重変換しない)。"""
    monkeypatch.setattr(clock, "_resolve_local_tz", lambda tz=None: _JST)
    ev_time = datetime(2026, 6, 29, 19, 0, 0)  # naive

    start, end = _econ_window_for(ev_time)

    assert start == datetime(2026, 6, 29, 18, 0, 0)
    assert end == datetime(2026, 6, 29, 20, 0, 0)
