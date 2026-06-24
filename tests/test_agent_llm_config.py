"""orchestrator 5-agent 個別 LLM 設定 (config/agents.yaml) のテスト。

AgentLlmConfig / OrchestratorAgentsLlmConfig の既定値と、loader による
agents.yaml の構築・検証を確認する。実 LLM 接続は起こさない。
"""
from __future__ import annotations

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
