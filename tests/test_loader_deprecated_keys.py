"""Loader の deprecated key 削除動作テスト。"""
from __future__ import annotations

import logging

from src.config.loader import _strip_deprecated_keys


def test_strip_deprecated_keys_removes_max_total_positions(caplog):
    trading_dict = {
        "risk_per_trade": 0.02,
        "max_total_positions": 4,
        "signal_confidence_threshold": 0.55,
    }
    with caplog.at_level(logging.WARNING):
        result = _strip_deprecated_keys(trading_dict)
    assert "max_total_positions" not in result
    assert any("max_total_positions" in r.message for r in caplog.records)


def test_strip_deprecated_keys_removes_all_three():
    trading_dict = {
        "max_total_positions": 4,
        "max_positions_per_currency_group": 2,
        "max_same_direction_per_group": 2,
    }
    result = _strip_deprecated_keys(trading_dict)
    assert result == {}


def test_strip_deprecated_keys_preserves_other_keys():
    trading_dict = {
        "risk_per_trade": 0.02,
        "max_total_positions": 4,           # deprecated
        "max_positions_per_pair": 2,        # 新規 (残す)
        "scale_in_enabled": False,          # 新規 (残す)
    }
    result = _strip_deprecated_keys(trading_dict)
    assert result == {
        "risk_per_trade": 0.02,
        "max_positions_per_pair": 2,
        "scale_in_enabled": False,
    }


def test_strip_deprecated_keys_no_op_when_no_deprecated(caplog):
    trading_dict = {
        "risk_per_trade": 0.02,
        "max_positions_per_pair": 2,
    }
    with caplog.at_level(logging.WARNING):
        result = _strip_deprecated_keys(trading_dict)
    assert result == trading_dict
    deprecated_warnings = [r for r in caplog.records if "deprecated" in r.message.lower()]
    assert len(deprecated_warnings) == 0


def test_strip_deprecated_keys_removes_initial_balance(caplog):
    """Task 9: initial_balance is deprecated (balance.json is the source of truth)."""
    trading_dict = {
        "initial_balance": 10000.0,
        "risk_per_trade": 0.02,
    }
    with caplog.at_level(logging.WARNING):
        result = _strip_deprecated_keys(trading_dict)
    assert "initial_balance" not in result
    assert result == {"risk_per_trade": 0.02}
    assert any("initial_balance" in r.message for r in caplog.records)


def test_strip_deprecated_keys_removes_drawdown_lookback_days(caplog):
    """Task 9: drawdown_kill_switch_lookback_days is deprecated (peak_balance is read directly)."""
    trading_dict = {
        "drawdown_kill_switch_lookback_days": 30,
        "drawdown_kill_switch_enabled": True,
    }
    with caplog.at_level(logging.WARNING):
        result = _strip_deprecated_keys(trading_dict)
    assert "drawdown_kill_switch_lookback_days" not in result
    assert result == {"drawdown_kill_switch_enabled": True}
    assert any("drawdown_kill_switch_lookback_days" in r.message for r in caplog.records)
