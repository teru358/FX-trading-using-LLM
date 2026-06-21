"""technical 収集 interval の config 既定値とスケジュール生成のテスト。"""
from __future__ import annotations

from src.config.schema import ScheduleConfig


def test_schedule_config_technical_intervals_default_to_hourly():
    """新規 interval フィールドは既定で 1 (= 毎時、現状維持)。"""
    cfg = ScheduleConfig()
    assert cfg.technical_trade_interval_hours == 1
    assert cfg.technical_watch_interval_hours == 1
