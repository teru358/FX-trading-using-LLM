"""Real AppConfig integration test for audit.

This test uses the actual load_config() path (not MagicMock) to catch
AttributeError bugs that mock-based tests miss.
"""
from __future__ import annotations

from pathlib import Path


def test_real_appconfig_has_audit_properties():
    """load_config() で作成した AppConfig が audit で必要な属性を持つ。"""
    from src.config import load_config

    cfg = load_config()

    # These properties must exist and return Path objects
    assert isinstance(cfg.config_dir, Path)
    assert isinstance(cfg.audit_output_dir, Path)
    assert isinstance(cfg.audit_lessons_path, Path)

    assert cfg.config_dir.name == "config"
    assert cfg.audit_output_dir.name == "audit"
    assert cfg.audit_lessons_path.name == "audit_lessons.md"
    assert cfg.audit_lessons_path.parent == cfg.config_dir
