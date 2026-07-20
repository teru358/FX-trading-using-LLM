"""orchestrator config block (spec §12) のパース・デフォルトテスト。"""
from __future__ import annotations

import pytest

from src.config.schema import OrchestratorConfig, OrchestratorFiringConfig
from src.config.loader import _build_orchestrator_config


def test_entry_min_rr_default_and_override() -> None:
    from src.config.schema import OrchestratorEntryConfig

    assert OrchestratorEntryConfig().min_rr == 1.5
    assert OrchestratorEntryConfig(min_rr=2.0).min_rr == 2.0


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_entry_min_rr_invalid_rejected(bad) -> None:
    from src.config.schema import OrchestratorEntryConfig

    with pytest.raises(ValueError, match="min_rr"):
        OrchestratorEntryConfig(min_rr=bad)


@pytest.mark.parametrize("field", ["spread_max_pips", "news_impact_min"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_entry_float_thresholds_reject_non_finite(field, bad) -> None:
    from src.config.schema import OrchestratorEntryConfig

    with pytest.raises(ValueError, match=field):
        OrchestratorEntryConfig(**{field: bad})


@pytest.mark.parametrize("field", ["spread_max_pips", "news_impact_min"])
def test_entry_float_thresholds_allow_zero_and_negative(field) -> None:
    # 負値/0 は fail-visible なので許容 (方針: 有限性のみ検証)。
    from src.config.schema import OrchestratorEntryConfig

    assert getattr(OrchestratorEntryConfig(**{field: 0.0}), field) == 0.0
    assert getattr(OrchestratorEntryConfig(**{field: -1.0}), field) == -1.0


def test_entry_price_move_pct_removed() -> None:
    # デッドフィールド削除の回帰防止 (再追加を検知)。
    import dataclasses
    from src.config.schema import OrchestratorEntryConfig

    field_names = {f.name for f in dataclasses.fields(OrchestratorEntryConfig)}
    assert "price_move_pct" not in field_names


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
        "mode": "shadow",
        "policy": {"trade_horizon": "day", "advice_memo": "wait for CPI"},
        "llm": {"max_concurrent_jobs": 1, "planning_timeout_seconds": 90},
        "locks": {"order_lock_ttl_seconds": 60},
        "entry": {"spread_max_pips": 1.5},
    }
    cfg = _build_orchestrator_config(data)
    assert cfg.enabled is True
    assert cfg.mode == "shadow"
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


def test_build_orchestrator_config_parses_firing_block() -> None:
    raw = {
        "enabled": True,
        "mode": "shadow",
        "pairs": ["USDJPY=X", "EURUSD=X"],
        "firing": {
            "material_news_impact_min": 0.6,
            "material_bias_delta_min": 0.25,
            "debounce_window_seconds": 120,
            "min_planning_interval_seconds": 900,
        },
        "entry": {"max_quote_age_seconds": 8, "max_technical_age_seconds": 1200},
    }
    cfg = _build_orchestrator_config(raw)
    assert cfg.pairs == ["USDJPY=X", "EURUSD=X"]
    assert cfg.firing.material_news_impact_min == 0.6
    assert cfg.firing.material_bias_delta_min == 0.25
    assert cfg.firing.debounce_window_seconds == 120
    assert cfg.firing.min_planning_interval_seconds == 900
    assert cfg.entry.max_quote_age_seconds == 8
    assert cfg.entry.max_technical_age_seconds == 1200


def test_build_orchestrator_config_pairs_defaults_empty() -> None:
    cfg = _build_orchestrator_config({})
    assert cfg.pairs == []
    assert cfg.firing.debounce_window_seconds == 180  # default


def test_orchestrator_hindsight_config_defaults() -> None:
    cfg = OrchestratorConfig()
    # 既定 horizon は audit_post_hoc の post_close_hours=24 と揃える (= 86400 秒)
    assert cfg.hindsight.horizon_seconds == 86400
    # poll loop の評価間隔 (秒)。24h 評価なので長めの既定で十分。
    assert cfg.hindsight.poll_interval_seconds == 600


def test_build_orchestrator_config_parses_hindsight_block() -> None:
    raw = {
        "enabled": True,
        "hindsight": {
            "horizon_seconds": 14400,
            "poll_interval_seconds": 60,
        },
    }
    cfg = _build_orchestrator_config(raw)
    assert cfg.hindsight.horizon_seconds == 14400
    assert cfg.hindsight.poll_interval_seconds == 60


def test_build_orchestrator_config_null_hindsight_uses_defaults() -> None:
    cfg = _build_orchestrator_config({"enabled": True, "hindsight": None})
    assert cfg.hindsight.horizon_seconds == 86400
    assert cfg.hindsight.poll_interval_seconds == 600


def test_orchestrator_notifications_config_defaults() -> None:
    cfg = OrchestratorConfig()
    # shadow 通知は既定で on (Phase 2 design §12)。各イベント個別フラグも既定 on。
    assert cfg.notifications.shadow_enabled is True
    assert cfg.notifications.shadow_plan_created is True
    assert cfg.notifications.shadow_triggered is True
    assert cfg.notifications.shadow_hindsight is True
    assert cfg.notifications.shadow_daily_summary is True


def test_build_orchestrator_config_parses_notifications_block() -> None:
    raw = {
        "enabled": True,
        "notifications": {
            "shadow_enabled": True,
            "shadow_plan_created": False,
            "shadow_triggered": True,
            "shadow_hindsight": False,
            "shadow_daily_summary": False,
        },
    }
    cfg = _build_orchestrator_config(raw)
    assert cfg.notifications.shadow_enabled is True
    assert cfg.notifications.shadow_plan_created is False
    assert cfg.notifications.shadow_triggered is True
    assert cfg.notifications.shadow_hindsight is False
    assert cfg.notifications.shadow_daily_summary is False


def test_build_orchestrator_config_null_notifications_uses_defaults() -> None:
    cfg = _build_orchestrator_config({"enabled": True, "notifications": None})
    assert cfg.notifications.shadow_enabled is True
    assert cfg.notifications.shadow_plan_created is True


# --- 外部レビュー Medium (修正2): mode の有効値を固定する ----------------------
#
# 実装 (schema.py の __post_init__) が受理するのは shadow / live のみ。
# observe は Task F 時点で執行段を起動しないため valid_modes から外されているが、
# フィールド宣言のコメントと deploy ノート / plan には observe が有効値として
# 残っていた。どちらが真かをテストで固定し、再発を防ぐ。


@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_orchestrator_mode_valid_values_accepted(mode) -> None:
    assert OrchestratorConfig(mode=mode).mode == mode


@pytest.mark.parametrize("mode", ["observe", "paper", "", "LIVE"])
def test_orchestrator_mode_invalid_values_rejected(mode) -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        OrchestratorConfig(mode=mode)
