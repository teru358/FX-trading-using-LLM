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


def test_build_technical_dispatch_routes_by_time():
    """union 時刻ごとに、trade 時刻のみ→trade、watch 時刻のみ→watch、両方→watch→trade 順。"""
    from src.jobs.technical_schedule import build_technical_dispatch

    calls = []
    times, dispatch = build_technical_dispatch(
        trade_set={"00:00", "01:00"}, watch_set={"00:00"},
        run_watch=lambda t: calls.append(("watch", t)),
        run_trade=lambda t: calls.append(("trade", t)),
    )

    assert times == ["00:00", "01:00"]
    dispatch("00:00")
    dispatch("01:00")
    assert calls == [("watch", "00:00"), ("trade", "00:00"), ("trade", "01:00")]


def test_build_technical_dispatch_watch_only_time():
    """watch のみの時刻では watch だけ実行する。"""
    from src.jobs.technical_schedule import build_technical_dispatch

    calls = []
    times, dispatch = build_technical_dispatch(
        trade_set={"00:00"}, watch_set={"06:00"},
        run_watch=lambda t: calls.append(("watch", t)),
        run_trade=lambda t: calls.append(("trade", t)),
    )
    assert times == ["00:00", "06:00"]
    dispatch("06:00")
    assert calls == [("watch", "06:00")]
