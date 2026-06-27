import pytest

from src.config.loader import _build_orchestrator_config
from src.config.schema import AppConfig, OrchestratorConfig


def test_orchestrator_mode_defaults_shadow():
    cfg = OrchestratorConfig()
    assert cfg.mode == "shadow"
    assert cfg.execution_opinion_recheck_enabled is False


def test_orchestrator_mode_accepts_shadow_and_live():
    for m in ("shadow", "live"):
        assert OrchestratorConfig(mode=m).mode == m


def test_orchestrator_mode_rejects_unknown():
    with pytest.raises(ValueError):
        OrchestratorConfig(mode="bogus")


def test_loader_passes_execution_opinion_recheck_enabled():
    """codex Medium: YAML の execution_opinion_recheck_enabled=true が loader 経由で
    OrchestratorConfig に届く (field 列挙漏れで常に default False になっていた回帰)。"""
    cfg = _build_orchestrator_config({"execution_opinion_recheck_enabled": True})
    assert cfg.execution_opinion_recheck_enabled is True
    # 省略時は default False
    assert _build_orchestrator_config({}).execution_opinion_recheck_enabled is False


# ── AppConfig cross-field validation (spec §5, 2026-06-25 改訂) ──


def _app(*, mode, live_broker=None, orch_mode="live"):
    return AppConfig(
        mode=mode,
        live_broker=live_broker,
        orchestrator=OrchestratorConfig(mode=orch_mode),
    )


def test_appconfig_orchestrator_live_paper_is_allowed():
    """orchestrator.mode=live + AppConfig.mode=paper は許可 (段階的 paper 検証、2026-06-25 改訂)。

    本番資金を動かさず paper_broker で F 経路を動作確認する正当な構成。ValueError にしない。
    """
    cfg = _app(mode="paper")
    assert cfg.mode == "paper"
    assert cfg.orchestrator.mode == "live"


def test_appconfig_orchestrator_live_live_test_is_allowed():
    """orchestrator.mode=live + AppConfig.mode=live_test も許可 (paper + observer 検証)。"""
    cfg = _app(mode="live_test")
    assert cfg.mode == "live_test"


def test_appconfig_orchestrator_live_with_top_level_live_requires_broker():
    """orchestrator.mode=live + AppConfig.mode=live + live_broker=None は ValueError。

    実発注モードなのに broker 未設定 = 取り違え事故を弾く (validation の本来の目的)。
    """
    with pytest.raises(ValueError):
        _app(mode="live", live_broker=None)


def test_appconfig_orchestrator_live_with_top_level_live_and_broker_ok():
    """orchestrator.mode=live + AppConfig.mode=live + live_broker=mt5 は成功 (本番発注構成)。"""
    cfg = _app(mode="live", live_broker="mt5")
    assert cfg.mode == "live"
    assert cfg.live_broker == "mt5"


def test_appconfig_orchestrator_shadow_with_live_no_broker_ok():
    """orchestrator.mode=shadow (既定) では AppConfig.mode=live + broker=None でも cross-field
    検証は走らない (この validation は orchestrator が発注主体のときだけ意味を持つ)。"""
    cfg = _app(mode="live", live_broker=None, orch_mode="shadow")
    assert cfg.mode == "live"
