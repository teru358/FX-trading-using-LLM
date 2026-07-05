"""分粒度スケジュール (spec S-4c / codex High#3)。"""
from src.jobs.technical_schedule import technical_times_for, technical_times_for_minutes
from src.config.schema import ScheduleConfig


def test_minutes_30_generates_hh_mm():
    times = technical_times_for_minutes(30)
    assert len(times) == 48
    assert times[0] == "00:00"
    assert times[1] == "00:30"
    assert "12:30" in times


def test_minutes_60_equals_hourly():
    assert technical_times_for_minutes(60) == technical_times_for(1)


def test_minutes_nonpositive_falls_back_to_60():
    assert technical_times_for_minutes(0) == technical_times_for_minutes(60)


def test_effective_trade_interval_seconds():
    cfg = ScheduleConfig()
    assert cfg.effective_trade_interval_seconds() == 3600  # hours=1, minutes 未設定
    cfg.technical_trade_interval_minutes = 30
    assert cfg.effective_trade_interval_seconds() == 1800  # minutes 優先


def test_effective_trade_times_minutes_priority():
    cfg = ScheduleConfig()
    cfg.technical_trade_interval_minutes = 30
    assert cfg.effective_trade_times() == technical_times_for_minutes(30)
    cfg.technical_trade_interval_minutes = None
    assert cfg.effective_trade_times() == technical_times_for(1)
