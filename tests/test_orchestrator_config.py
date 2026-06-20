"""orchestrator config block (spec §12) のパース・デフォルトテスト。"""
from __future__ import annotations

from src.config.schema import OrchestratorConfig, OrchestratorFiringConfig
from src.config.loader import _build_orchestrator_config


def test_orchestrator_config_defaults() -> None:
    cfg = OrchestratorConfig()
    assert cfg.enabled is False
    assert cfg.mode == "shadow"
    assert cfg.policy.trade_horizon == "swing"
    assert cfg.llm.max_concurrent_jobs == 1
    assert cfg.llm.planning_timeout_seconds == 180
    assert cfg.locks.order_lock_ttl_seconds == 120


def test_build_orchestrator_config_from_yaml_dict() -> None:
    data = {
        "enabled": True,
        "mode": "observe",
        "policy": {"trade_horizon": "day", "advice_memo": "wait for CPI"},
        "llm": {"max_concurrent_jobs": 1, "planning_timeout_seconds": 90},
        "locks": {"order_lock_ttl_seconds": 60},
        "entry": {"spread_max_pips": 1.5},
    }
    cfg = _build_orchestrator_config(data)
    assert cfg.enabled is True
    assert cfg.mode == "observe"
    assert cfg.policy.trade_horizon == "day"
    assert cfg.policy.advice_memo == "wait for CPI"
    assert cfg.llm.planning_timeout_seconds == 90
    assert cfg.locks.order_lock_ttl_seconds == 60
    assert cfg.entry.spread_max_pips == 1.5


def test_build_orchestrator_config_empty_dict_uses_defaults() -> None:
    cfg = _build_orchestrator_config({})
    assert cfg.enabled is False
    assert cfg.mode == "shadow"
    # ネストブロックも dataclass デフォルトに落ちる
    assert cfg.llm.max_concurrent_jobs == 1
    assert cfg.locks.order_lock_ttl_seconds == 120
    assert cfg.agents.audit_enabled is True


def test_build_orchestrator_config_null_subkeys_use_defaults() -> None:
    """YAML で `llm:` のように値なしキーを書くと None になる。`or {}` で
    dataclass デフォルトに落ちることを保証する (None を _from_dict に渡さない)。"""
    cfg = _build_orchestrator_config(
        {"enabled": True, "llm": None, "policy": None, "agents": None}
    )
    assert cfg.enabled is True
    assert cfg.llm.max_concurrent_jobs == 1          # None → デフォルト
    assert cfg.policy.trade_horizon == "swing"       # None → デフォルト
    assert cfg.agents.news_enabled is True           # None → デフォルト


def test_orchestrator_firing_config_defaults() -> None:
    cfg = OrchestratorConfig()
    assert cfg.firing.material_news_impact_min == 0.5
    assert cfg.firing.material_bias_delta_min == 0.20
    assert cfg.firing.debounce_window_seconds == 180
    assert cfg.firing.min_planning_interval_seconds == 1800


def test_orchestrator_pairs_default_empty() -> None:
    cfg = OrchestratorConfig()
    assert cfg.pairs == []
