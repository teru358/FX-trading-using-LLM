"""tick_migration_stage / quote_stream_poll_seconds の schema + loader テスト (review H-a)。"""
import pytest

from src.config.schema import OrchestratorConfig
from src.config.loader import _build_orchestrator_config


def test_tick_migration_stage_defaults_off():
    cfg = OrchestratorConfig()
    assert cfg.tick_migration_stage == "off"
    assert cfg.quote_stream_poll_seconds == 2


def test_tick_migration_stage_accepts_known_values():
    # protect_* は enabled=True 必須 (worker は orchestrator runtime 内でのみ構築される)。
    for stage in ("off", "producer", "protect_shadow", "protect_live"):
        cfg = OrchestratorConfig(enabled=True, tick_migration_stage=stage)
        assert cfg.tick_migration_stage == stage


def test_invalid_stage_rejected():
    with pytest.raises(ValueError):
        OrchestratorConfig(tick_migration_stage="bogus")


def test_invalid_poll_seconds_rejected():
    with pytest.raises(ValueError):
        OrchestratorConfig(quote_stream_poll_seconds=0)


def test_loader_reads_stage_from_yaml():
    """YAML ブロックから stage / poll が読まれる (review H-a 回帰)。"""
    cfg = _build_orchestrator_config(
        {"tick_migration_stage": "producer", "quote_stream_poll_seconds": 3}
    )
    assert cfg.tick_migration_stage == "producer"
    assert cfg.quote_stream_poll_seconds == 3


def test_loader_defaults_when_absent():
    cfg = _build_orchestrator_config({})
    assert cfg.tick_migration_stage == "off"
    assert cfg.quote_stream_poll_seconds == 2


def test_protect_stage_requires_enabled():
    for stage in ("protect_shadow", "protect_live"):
        with pytest.raises(ValueError):
            OrchestratorConfig(enabled=False, tick_migration_stage=stage)


def test_protect_stage_ok_when_enabled():
    for stage in ("protect_shadow", "protect_live"):
        cfg = OrchestratorConfig(enabled=True, tick_migration_stage=stage)
        assert cfg.tick_migration_stage == stage


def test_producer_stage_does_not_require_enabled():
    # producer 段は worker 不要 (price_monitor は execute=True を維持) なので enabled 不問
    cfg = OrchestratorConfig(enabled=False, tick_migration_stage="producer")
    assert cfg.tick_migration_stage == "producer"
