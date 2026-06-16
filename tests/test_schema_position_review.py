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


def test_position_management_v2_defaults() -> None:
    cfg = TradingConfig()
    assert cfg.reversal_guard_enabled is True
    assert cfg.reversal_close_enabled is False
    assert cfg.reversal_consecutive_required == 2
    assert cfg.reversal_raise_sl_to_breakeven is True
    assert cfg.time_stop_enabled is True
    assert cfg.no_progress_enabled is True
    assert cfg.no_progress_watch_hours == 6
    assert cfg.no_progress_exit_hours == 12
    assert cfg.no_progress_min_mfe_r == 0.1
    assert cfg.no_progress_requires_signal_weakness is True
    assert cfg.stale_position_review_hours == 24
    assert cfg.timeout_cooldown_hours == 4
    assert cfg.stale_signal_hours == 8
    assert cfg.session_end_flatten_enabled is False
    assert cfg.profit_protection_enabled is True
    assert cfg.protect_half_r == 0.3
    assert cfg.protect_breakeven_r == 0.5
    assert cfg.protect_lock_r == 1.0
    assert cfg.giveback_close_r == 0.4
    assert cfg.giveback_close_min_mfe_r == 0.8
