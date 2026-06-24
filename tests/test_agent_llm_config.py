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
