"""PriceMonitorConfig の trailing_stop 設定バリデーションテスト。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from src.config import BASE_DIR, load_config


def _copy_real_config_and_patch(tmp_path: Path, trailing_overrides: dict) -> Path:
    """本番 config/ を tmp_path にコピーし、price_monitor.trailing_stop_* を上書きする。

    実設定を複製することで、バリデーション以外のセクション不足で load_config が
    失敗するのを防ぐ。
    """
    src_config_dir = BASE_DIR / "config"
    dst_config_dir = tmp_path / "config"
    shutil.copytree(src_config_dir, dst_config_dir)

    settings_path = dst_config_dir / "settings.yaml"
    with open(settings_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    pm = data.setdefault("price_monitor", {})
    pm["trailing_stop_enabled"] = True
    for k, v in trailing_overrides.items():
        pm[k] = v

    with open(settings_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)

    return settings_path


def test_valid_trailing_config_loads(tmp_path):
    """breakeven < activation なら正常に読み込める。"""
    settings_path = _copy_real_config_and_patch(
        tmp_path,
        {"trailing_stop_breakeven_pct": 0.20, "trailing_stop_activation_pct": 0.40},
    )
    cfg = load_config(settings_path)
    assert cfg.price_monitor.trailing_stop_breakeven_pct == 0.20
    assert cfg.price_monitor.trailing_stop_activation_pct == 0.40


def test_breakeven_equal_to_activation_raises(tmp_path):
    """breakeven == activation は ValueError。"""
    settings_path = _copy_real_config_and_patch(
        tmp_path,
        {"trailing_stop_breakeven_pct": 0.40, "trailing_stop_activation_pct": 0.40},
    )
    with pytest.raises(ValueError, match="breakeven_pct"):
        load_config(settings_path)


def test_breakeven_greater_than_activation_raises(tmp_path):
    """breakeven > activation は ValueError。"""
    settings_path = _copy_real_config_and_patch(
        tmp_path,
        {"trailing_stop_breakeven_pct": 0.50, "trailing_stop_activation_pct": 0.40},
    )
    with pytest.raises(ValueError, match="breakeven_pct"):
        load_config(settings_path)


def test_breakeven_zero_raises(tmp_path):
    """breakeven == 0 は ValueError。"""
    settings_path = _copy_real_config_and_patch(
        tmp_path,
        {"trailing_stop_breakeven_pct": 0.0, "trailing_stop_activation_pct": 0.40},
    )
    with pytest.raises(ValueError, match="breakeven_pct"):
        load_config(settings_path)
