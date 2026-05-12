"""TradingConfig 新規フィールドのデフォルト検証。"""
from src.config.schema import TradingConfig


def test_reversal_min_holding_minutes_default_is_240() -> None:
    cfg = TradingConfig()
    assert cfg.reversal_min_holding_minutes == 240


def test_reversal_min_holding_minutes_is_overridable() -> None:
    cfg = TradingConfig(reversal_min_holding_minutes=60)
    assert cfg.reversal_min_holding_minutes == 60


def test_remote_sl_sync_disabled_by_default() -> None:
    cfg = TradingConfig()
    assert cfg.remote_sl_sync_enabled is False


def test_remote_sl_sync_can_be_enabled() -> None:
    cfg = TradingConfig(remote_sl_sync_enabled=True)
    assert cfg.remote_sl_sync_enabled is True
