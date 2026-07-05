"""FX staleness の config 化 (spec S-3): 既定 360min = 現行 6h と等価で挙動不変。"""
from datetime import timedelta

from src.config.schema import AppConfig
from src.jobs.technical_collector import _max_staleness_for


class _FxInst:
    asset_type = "fx"


class _EtfInst:
    asset_type = "equity"


def test_default_is_6h_equivalent():
    cfg = AppConfig()
    assert _max_staleness_for(_FxInst(), cfg) == timedelta(hours=6)


def test_day_value_90min():
    cfg = AppConfig()
    cfg.schedule.technical_max_staleness_fx_minutes = 90
    assert _max_staleness_for(_FxInst(), cfg) == timedelta(minutes=90)


def test_watch_side_unchanged():
    cfg = AppConfig()
    cfg.schedule.technical_max_staleness_fx_minutes = 90
    assert _max_staleness_for(_EtfInst(), cfg) == timedelta(hours=120)
