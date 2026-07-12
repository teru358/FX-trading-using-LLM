# tests/test_technical_delay_config.py
import pytest

from src.config.schema import ScheduleConfig


def test_technical_delay_default_is_5s():
    cfg = ScheduleConfig()
    assert cfg.technical_inter_pair_delay_seconds == 5


def test_technical_delay_range_validated():
    with pytest.raises(ValueError):
        ScheduleConfig(technical_inter_pair_delay_seconds=61)
    with pytest.raises(ValueError):
        ScheduleConfig(technical_inter_pair_delay_seconds=-1)
    ScheduleConfig(technical_inter_pair_delay_seconds=0)   # 境界 OK
    ScheduleConfig(technical_inter_pair_delay_seconds=60)  # 境界 OK


def test_collector_uses_schedule_delay():
    import inspect
    from src.jobs import technical_collector as tc
    src = inspect.getsource(tc)
    assert "schedule.technical_inter_pair_delay_seconds" in src
    assert "news_collection.inter_pair_delay_seconds" not in src
