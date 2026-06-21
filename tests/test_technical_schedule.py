"""technical 収集 interval の config 既定値とスケジュール生成のテスト。"""
from __future__ import annotations

from src.config.schema import ScheduleConfig


def test_schedule_config_technical_intervals_default_to_hourly():
    """新規 interval フィールドは既定で 1 (= 毎時、現状維持)。"""
    cfg = ScheduleConfig()
    assert cfg.technical_trade_interval_hours == 1
    assert cfg.technical_watch_interval_hours == 1


def test_technical_times_for_hourly():
    from src.jobs.technical_schedule import technical_times_for
    times = technical_times_for(1)
    assert len(times) == 24
    assert times[0] == "00:00"
    assert times[-1] == "23:00"


def test_technical_times_for_every_two_hours():
    from src.jobs.technical_schedule import technical_times_for
    times = technical_times_for(2)
    assert times == ["00:00", "02:00", "04:00", "06:00", "08:00", "10:00",
                     "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]


def test_technical_times_for_zero_or_negative_falls_back_to_hourly():
    """0 や負値は 1 (毎時) に倒す (range step >= 1 ガード)。"""
    from src.jobs.technical_schedule import technical_times_for
    assert len(technical_times_for(0)) == 24
    assert len(technical_times_for(-3)) == 24
