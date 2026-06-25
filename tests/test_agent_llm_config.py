"""orchestrator 5-agent 個別 LLM 設定 (config/agents.yaml) のテスト。

AgentLlmConfig / OrchestratorAgentsLlmConfig の既定値と、loader による
agents.yaml の構築・検証を確認する。実 LLM 接続は起こさない。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from src.config import BASE_DIR, ConfigError, load_config
from src.config.schema import AgentLlmConfig, OrchestratorAgentsLlmConfig


def test_agent_llm_config_defaults() -> None:
    cfg = AgentLlmConfig()
    assert cfg.provider == ""        # 空欄 = fallback
    assert cfg.model == ""
    assert cfg.temperature == 0.2


def test_orchestrator_agents_llm_config_defaults() -> None:
    cfg = OrchestratorAgentsLlmConfig()
    # 全 5 agent が AgentLlmConfig 既定 (provider 空 = fallback)
    for name in ("planner", "news", "technical", "execution_opinion", "context_summary"):
        agent = getattr(cfg, name)
        assert isinstance(agent, AgentLlmConfig)
        assert agent.provider == ""


def test_llm_config_has_empty_provider_configs_by_default() -> None:
    from src.config.schema import LLMConfig

    cfg = LLMConfig()
    assert cfg.provider_configs == {}        # 既定は空 dict (後方互換)
    # 既存 provider_config (単一) は維持
    assert cfg.provider_config is not None


def test_app_config_has_default_agent_llms() -> None:
    from src.config.schema import AppConfig, OrchestratorAgentsLlmConfig

    cfg = AppConfig()
    assert isinstance(cfg.agent_llms, OrchestratorAgentsLlmConfig)
    # 既定では全 agent が fallback (provider 空)
    assert cfg.agent_llms.planner.provider == ""


def test_merge_split_configs_picks_up_agents_yaml(tmp_path) -> None:
    """config/agents.yaml があれば base['agents'] に top-level merge される。"""
    from src.config.loader import _merge_split_configs

    (tmp_path / "agents.yaml").write_text(
        "agents:\n"
        "  planner:\n"
        "    provider: claude-cli\n"
        "    model: claude-sonnet-4-6\n"
        "    temperature: 0.1\n",
        encoding="utf-8",
    )
    merged = _merge_split_configs({}, tmp_path)
    assert "agents" in merged
    assert merged["agents"]["planner"]["model"] == "claude-sonnet-4-6"


def _write_with_agents(tmp_path: Path, agents_yaml: str | None) -> Path:
    """本番 config/ を tmp にコピーし、agents.yaml を差し込む (None なら作らない)。"""
    src_config_dir = BASE_DIR / "config"
    dst_config_dir = tmp_path / "config"
    shutil.copytree(src_config_dir, dst_config_dir)
    agents_path = dst_config_dir / "agents.yaml"
    if agents_yaml is None:
        agents_path.unlink(missing_ok=True)
    else:
        agents_path.write_text(agents_yaml, encoding="utf-8")
    return dst_config_dir / "settings.yaml"


def test_build_agent_llms_from_yaml(tmp_path) -> None:
    settings = _write_with_agents(
        tmp_path,
        "agents:\n"
        "  planner:\n"
        "    provider: claude-cli\n"
        "    model: claude-sonnet-4-6\n"
        "    temperature: 0.15\n",
    )
    # cross-provider (top-level=llamacpp) なので provider_configs に claude-cli を登録
    data = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    data.setdefault("llm", {})["provider_configs"] = {"claude-cli": {}}
    settings.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    cfg = load_config(settings)
    assert cfg.agent_llms.planner.provider == "claude-cli"
    assert cfg.agent_llms.planner.model == "claude-sonnet-4-6"
    assert cfg.agent_llms.planner.temperature == 0.15
    # 未指定 agent は fallback 既定
    assert cfg.agent_llms.news.provider == ""


def test_missing_agents_yaml_yields_all_fallback(tmp_path) -> None:
    settings = _write_with_agents(tmp_path, None)  # agents.yaml 無し
    cfg = load_config(settings)
    for name in ("planner", "news", "technical", "execution_opinion", "context_summary"):
        assert getattr(cfg.agent_llms, name).provider == ""


def test_unknown_agent_key_raises(tmp_path) -> None:
    """typo した agent キー (例: planer) は起動時 ConfigError (silent fallback を防ぐ)。"""
    settings = _write_with_agents(
        tmp_path,
        "agents:\n"
        "  planer:\n"            # typo of 'planner'
        "    provider: claude-cli\n"
        "    model: x\n",
    )
    with pytest.raises(ConfigError, match="unknown agent"):
        load_config(settings)


def test_agent_unknown_provider_raises(tmp_path) -> None:
    """agent の provider が LLM_PROVIDERS 外なら起動時 ConfigError。"""
    settings = _write_with_agents(
        tmp_path,
        "agents:\n"
        "  technical:\n"
        "    provider: bogus\n"   # 不正 provider
        "    model: x\n",
    )
    with pytest.raises(ConfigError, match="Unknown provider"):
        load_config(settings)


def test_agent_provider_without_model_raises(tmp_path) -> None:
    """provider 指定 + model 欠落は起動時 ConfigError (既存役割の model 必須と整合)。"""
    settings = _write_with_agents(
        tmp_path,
        "agents:\n"
        "  news:\n"
        "    provider: claude-cli\n"
        "    model: ''\n",        # 欠落
    )
    with pytest.raises(ConfigError, match="model is required"):
        load_config(settings)


def test_provider_configs_built_per_provider(tmp_path) -> None:
    """llm.provider_configs の各エントリが ProviderConfig に構築される。"""
    src_config_dir = BASE_DIR / "config"
    dst = tmp_path / "config"
    shutil.copytree(src_config_dir, dst)
    settings_path = dst / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    data.setdefault("llm", {})["provider_configs"] = {
        "claude-cli": {"command": "", "timeout_seconds": 90},
        "llamacpp": {"base_url": "http://localhost:8080/v1"},
    }
    settings_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    cfg = load_config(settings_path)
    assert "llamacpp" in cfg.llm.provider_configs
    assert cfg.llm.provider_configs["llamacpp"].base_url == "http://localhost:8080/v1"
    # claude-cli の command 空欄は "claude" 補完される
    assert cfg.llm.provider_configs["claude-cli"].command == "claude"
    assert cfg.llm.provider_configs["claude-cli"].timeout_seconds == 90


def test_provider_configs_llamacpp_missing_base_url_raises(tmp_path) -> None:
    """provider_configs.llamacpp に base_url が無ければ起動時 ConfigError。"""
    from src.config import ConfigError

    src_config_dir = BASE_DIR / "config"
    dst = tmp_path / "config"
    shutil.copytree(src_config_dir, dst)
    settings_path = dst / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    data.setdefault("llm", {})["provider_configs"] = {
        "llamacpp": {"base_url": ""},   # 欠落
    }
    settings_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError, match="base_url is required"):
        load_config(settings_path)


def test_agent_cross_provider_requires_provider_config(tmp_path) -> None:
    """agent.provider が top-level と異なり provider_configs に無ければ起動時 ConfigError。

    本番 settings の top-level provider は llamacpp。technical に claude-cli を
    指定しても provider_configs に claude-cli が無ければ弾く (未配線 agent でも検証)。
    """
    settings = _write_with_agents(
        tmp_path,
        "agents:\n"
        "  technical:\n"
        "    provider: claude-cli\n"   # top-level (llamacpp) と異なる
        "    model: claude-sonnet-4-6\n"
        "    temperature: 0.1\n",
    )
    # settings.yaml の top-level provider は llamacpp。provider_configs に claude-cli は未定義。
    with pytest.raises(ConfigError, match="provider_configs"):
        load_config(settings)


def test_agent_cross_provider_ok_when_provider_config_present(tmp_path) -> None:
    """provider_configs に該当 provider があれば cross-provider agent も通る。"""
    src_config_dir = BASE_DIR / "config"
    dst = tmp_path / "config"
    shutil.copytree(src_config_dir, dst)
    settings_path = dst / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    # top-level は llamacpp。cross-provider の claude-cli を provider_configs に追加。
    # claude-cli は base_url 不要、command 空欄は "claude" 補完されるので空 dict で valid。
    data.setdefault("llm", {})["provider_configs"] = {
        "claude-cli": {},
    }
    settings_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (dst / "agents.yaml").write_text(
        "agents:\n"
        "  technical:\n"
        "    provider: claude-cli\n"
        "    model: claude-sonnet-4-6\n",
        encoding="utf-8",
    )
    cfg = load_config(settings_path)
    assert cfg.agent_llms.technical.provider == "claude-cli"


def test_agent_same_provider_as_top_level_needs_no_provider_config(tmp_path) -> None:
    """agent.provider == top-level provider なら provider_configs 不要 (既存設定流用)。"""
    settings = _write_with_agents(
        tmp_path,
        "agents:\n"
        "  planner:\n"
        "    provider: llamacpp\n"   # top-level と同じ (本番は llamacpp)
        "    model: plutus\n",
    )
    cfg = load_config(settings)
    assert cfg.agent_llms.planner.provider == "llamacpp"


def test_new_dataclasses_reexported_from_package_root() -> None:
    """from src.config import ... で新 dataclass が import できる (入口の整合)。"""
    import src.config as cfg_pkg

    from src.config import AgentLlm, AgentLlmConfig, OrchestratorAgentsLlmConfig

    for name in ("AgentLlm", "AgentLlmConfig", "OrchestratorAgentsLlmConfig"):
        assert name in cfg_pkg.__all__
